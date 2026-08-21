"""Application-owned provider execution contracts."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from unittest.mock import MagicMock

import pytest

from my_claude_code.application.execution import (
    AttemptResultObserver,
    ProviderExecutor,
    RouteAttemptRecord,
    RouteExecutionPolicy,
)
from my_claude_code.application.ports import ProviderPort
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.async_iterators import AsyncCloseable
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import ReasoningPolicy


class FakeProvider:
    def __init__(self) -> None:
        self.preflight_calls: list[tuple[MessagesRequest, ReasoningPolicy]] = []
        self.stream_calls: list[dict[str, object]] = []
        self.stream_close_calls = 0

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy,
    ) -> None:
        self.preflight_calls.append((request, reasoning))

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls.append(
            {
                "request": request,
                "input_tokens": input_tokens,
                "request_id": request_id,
                "reasoning": reasoning,
            }
        )
        try:
            yield "event: message_stop\ndata: {}\n\n"
        finally:
            self.stream_close_calls += 1


class FailingPreflightProvider(FakeProvider):
    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy,
    ) -> None:
        raise ValueError("invalid provider request")


class FailingStreamConstructionProvider(FakeProvider):
    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        raise RuntimeError("stream construction failed")


def _routed_request(
    provider_id: str = "provider",
    provider_model: str = "provider-model",
    *,
    stream: bool = True,
) -> RoutedMessagesRequest:
    request = MessagesRequest(
        model=provider_model,
        messages=[Message(role="user", content="hello")],
        stream=stream,
    )
    return RoutedMessagesRequest(
        request=request,
        resolved=ResolvedModel(
            original_model="gateway-model",
            provider_id=provider_id,
            provider_model=provider_model,
            provider_model_ref=f"{provider_id}/{provider_model}",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
        requested_reasoning=ReasoningPolicy.on(),
    )


def _plan(*routed: RoutedMessagesRequest) -> RoutedMessagesPlan:
    return RoutedMessagesPlan(routed or (_routed_request(),))


@pytest.mark.asyncio
async def test_executor_uses_structural_provider_port_and_preflights_eagerly() -> None:
    provider = FakeProvider()
    routed = _routed_request()
    request = routed.request
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _messages, _system, _tools: 17,
    )

    stream = executor.stream(
        _plan(routed),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload=request.model_dump(),
        request_id="req_application",
    )

    assert provider.preflight_calls == [(request, ReasoningPolicy.on())]
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert provider.stream_calls == [
        {
            "request": request,
            "input_tokens": 17,
            "request_id": "req_application",
            "reasoning": ReasoningPolicy.on(),
        }
    ]
    assert provider.stream_close_calls == 1


@pytest.mark.asyncio
async def test_closing_executor_stream_closes_provider_stream_once() -> None:
    provider = FakeProvider()
    routed = _routed_request()
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _messages, _system, _tools: 17,
    )
    stream = executor.stream(
        _plan(routed),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_early_close",
    )

    assert await anext(stream) == "event: message_stop\ndata: {}\n\n"
    assert isinstance(stream, AsyncCloseable)
    await stream.aclose()

    assert provider.stream_close_calls == 1


@pytest.mark.asyncio
async def test_stream_construction_failure_remains_deferred_to_iteration() -> None:
    provider = FailingStreamConstructionProvider()
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _messages, _system, _tools: 17,
    )

    stream = executor.stream(
        _plan(),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_deferred_construction",
    )

    with pytest.raises(RuntimeError, match="stream construction failed"):
        await anext(stream)


def test_executor_preflight_failure_stays_before_token_count_and_stream() -> None:
    provider = FailingPreflightProvider()
    token_counter = MagicMock(return_value=17)
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=token_counter,
    )

    with pytest.raises(ValueError, match="invalid provider request"):
        executor.stream(
            _plan(),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_application",
        )

    token_counter.assert_not_called()
    assert provider.stream_calls == []


class ScriptedProvider(FakeProvider):
    """Provider whose stream fails after a set number of emitted chunks."""

    def __init__(self, *, chunks: tuple[str, ...], error: Exception | None) -> None:
        super().__init__()
        self._chunks = chunks
        self._error = error

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"request": request, "request_id": request_id})
        try:
            for chunk in self._chunks:
                yield chunk
            if self._error is not None:
                raise self._error
        finally:
            self.stream_close_calls += 1


def _executor(providers: Mapping[str, ProviderPort]) -> ProviderExecutor:
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 17,
    )


@pytest.mark.asyncio
async def test_fallback_runs_when_primary_fails_before_the_first_chunk() -> None:
    primary = ScriptedProvider(chunks=(), error=RuntimeError("upstream 503"))
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})
    attempts: list[tuple[int, str]] = []

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_fallback",
        on_attempt=lambda routed, index: attempts.append(
            (index, routed.resolved.provider_model_ref)
        ),
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert attempts == [(0, "primary/big"), (1, "secondary/small")]
    assert primary.stream_close_calls == 1
    assert secondary.stream_close_calls == 1


@pytest.mark.asyncio
async def test_failure_after_the_first_chunk_is_never_retried_on_a_fallback() -> None:
    """A streaming client has already seen the chunk; a second model would splice."""
    primary = ScriptedProvider(
        chunks=("event: a\n\n",), error=RuntimeError("mid-stream")
    )
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_committed",
    )

    assert await anext(stream) == "event: a\n\n"
    with pytest.raises(RuntimeError, match="mid-stream"):
        await anext(stream)
    assert secondary.stream_calls == []


@pytest.mark.asyncio
async def test_non_streaming_request_falls_back_after_the_first_chunk() -> None:
    """Nothing reached the client, so a mid-stream failure is still recoverable.

    A non-streaming client is served one aggregated message at the end. Treating
    the provider's first chunk as a commit made a fallback chain useless for
    every failure past time-to-first-token, which is where they mostly happen.
    """
    primary = ScriptedProvider(
        chunks=("event: partial\n\n",), error=RuntimeError("mid-stream")
    )
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})
    attempts: list[tuple[int, str]] = []

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big", stream=False),
            _routed_request("secondary", "small", stream=False),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_non_streaming_fallback",
        on_attempt=lambda routed, index: attempts.append(
            (index, routed.resolved.provider_model_ref)
        ),
    )

    # The failed attempt's partial output is dropped with it: the aggregator
    # must never see two openings spliced into one message.
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert attempts == [(0, "primary/big"), (1, "secondary/small")]
    assert primary.stream_close_calls == 1


@pytest.mark.asyncio
async def test_every_attempt_is_announced_even_when_the_chain_is_exhausted() -> None:
    """The request log must name the last model tried, not the first."""
    providers = {
        "primary": FailingPreflightProvider(),
        "secondary": FailingPreflightProvider(),
    }
    executor = _executor(providers)
    attempts: list[tuple[int, str]] = []

    with pytest.raises(ValueError, match="invalid provider request"):
        executor.stream(
            _plan(
                _routed_request("primary", "big"),
                _routed_request("secondary", "small"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_exhausted",
            on_attempt=lambda routed, index: attempts.append(
                (index, routed.resolved.provider_model_ref)
            ),
        )

    assert attempts == [(0, "primary/big"), (1, "secondary/small")]


@pytest.mark.asyncio
async def test_preflight_failure_moves_to_the_next_attempt() -> None:
    primary = FailingPreflightProvider()
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_preflight_fallback",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert primary.stream_calls == []
    assert secondary.stream_calls != []


def test_every_attempt_failing_preflight_raises_the_last_error_synchronously() -> None:
    providers = {
        "primary": FailingPreflightProvider(),
        "secondary": FailingPreflightProvider(),
    }
    executor = _executor(providers)

    with pytest.raises(ValueError, match="invalid provider request"):
        executor.stream(
            _plan(
                _routed_request("primary", "big"),
                _routed_request("secondary", "small"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_all_preflight_fail",
        )


@pytest.mark.asyncio
async def test_last_attempt_failure_propagates_its_own_error() -> None:
    primary = ScriptedProvider(chunks=(), error=RuntimeError("first down"))
    secondary = ScriptedProvider(chunks=(), error=RuntimeError("second down"))
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_all_fail",
    )

    with pytest.raises(RuntimeError, match="second down"):
        await anext(stream)


def test_a_plan_needs_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="at least one attempt"):
        RoutedMessagesPlan(())


class StallingProvider(FakeProvider):
    """Opens a stream, then produces nothing -- the shape a deadline exists for."""

    def __init__(self, *, stall_seconds: float = 3600.0, before: tuple[str, ...] = ()):
        super().__init__()
        self._stall_seconds = stall_seconds
        self._before = before

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"request": request, "request_id": request_id})
        try:
            for chunk in self._before:
                yield chunk
            await asyncio.sleep(self._stall_seconds)
            yield "event: never\n\n"
        finally:
            self.stream_close_calls += 1


def _deadline_executor(
    providers: Mapping[str, ProviderPort],
    *,
    first_token_timeout: float = 0.05,
    total_timeout: float = 0.0,
    health: RouteHealthRegistry | None = None,
) -> ProviderExecutor:
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 17,
        policy=RouteExecutionPolicy(
            first_token_timeout=first_token_timeout,
            total_timeout=total_timeout,
        ),
        health=health or RouteHealthRegistry(eject_after_failures=0),
    )


@pytest.mark.asyncio
async def test_a_model_that_sends_no_first_token_hands_over_to_the_fallback() -> None:
    """Nothing reached the client, so swapping models is invisible to it."""
    primary = StallingProvider()
    secondary = FakeProvider()
    executor = _deadline_executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_ttft",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert primary.stream_close_calls == 1


@pytest.mark.asyncio
async def test_the_first_token_deadline_stops_applying_once_output_started() -> None:
    """A slow generation is not a stalled one; only the total budget bounds it."""
    primary = StallingProvider(stall_seconds=0.2, before=("event: a\n\n",))
    secondary = FakeProvider()
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary},
        first_token_timeout=0.05,
        total_timeout=0.0,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_slow",
    )

    chunks = [chunk async for chunk in stream]
    assert chunks[0] == "event: a\n\n"
    assert secondary.stream_calls == []


@pytest.mark.asyncio
async def test_a_committed_stall_ends_at_the_total_budget() -> None:
    """No chain can rescue a committed stream, but it must still stop."""
    primary = StallingProvider(before=("event: a\n\n",))
    executor = _deadline_executor(
        {"primary": primary},
        first_token_timeout=0.0,
        total_timeout=0.05,
    )

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_budget",
    )

    # Drained by hand: what reached the client *before* the failure is the
    # assertion, and a comprehension would discard it along with the exception.
    chunks = stream.__aiter__()
    received: list[str] = []
    with pytest.raises(ExecutionFailure) as failure:
        while True:
            received.append(await anext(chunks))

    assert received == ["event: a\n\n"]
    assert failure.value.kind is FailureKind.TIMEOUT


@pytest.mark.asyncio
async def test_the_chain_is_not_extended_once_the_budget_is_spent() -> None:
    """Starting another model with no time left only delays the same error."""
    primary = StallingProvider()
    secondary = FakeProvider()
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary},
        first_token_timeout=0.05,
        total_timeout=0.05,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_spent",
    )

    with pytest.raises(ExecutionFailure):
        async for _chunk in stream:
            pass
    assert secondary.stream_calls == []


@pytest.mark.asyncio
async def test_deadlines_disabled_never_abandon_an_attempt() -> None:
    primary = StallingProvider(stall_seconds=0.05, before=("event: a\n\n",))
    executor = _deadline_executor(
        {"primary": primary}, first_token_timeout=0.0, total_timeout=0.0
    )

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_off",
    )

    assert [chunk async for chunk in stream] == ["event: a\n\n", "event: never\n\n"]


@pytest.mark.asyncio
async def test_an_upstream_timeout_is_not_reported_as_a_routing_deadline() -> None:
    """The upstream giving up and us declining to wait are different facts."""
    primary = ScriptedProvider(chunks=(), error=TimeoutError("upstream read timeout"))
    secondary = FakeProvider()
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary},
        first_token_timeout=30.0,
        total_timeout=30.0,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_upstream_timeout",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]


@pytest.mark.asyncio
async def test_a_model_benched_by_earlier_failures_is_skipped_entirely() -> None:
    """The point of ejection: the fallback answers without re-paying the timeout."""
    primary = FakeProvider()
    secondary = FakeProvider()
    health = RouteHealthRegistry(eject_after_failures=1, eject_seconds=300.0)
    health.record_failure("primary/big")
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary}, health=health
    )
    attempts: list[tuple[int, str]] = []

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_ejected",
        on_attempt=lambda routed, index: attempts.append(
            (index, routed.resolved.provider_model_ref)
        ),
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert primary.stream_calls == []
    assert attempts == [(1, "secondary/small")]


@pytest.mark.asyncio
async def test_a_served_request_clears_the_models_failure_streak() -> None:
    provider = FakeProvider()
    health = RouteHealthRegistry(eject_after_failures=2, eject_seconds=300.0)
    health.record_failure("primary/big")
    executor = _deadline_executor({"primary": provider}, health=health)

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_recovered",
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    health.record_failure("primary/big")
    assert not health.is_ejected("primary/big")


# ---------------------------------------------------------------- attempts --
#
# The chain's own account of itself. ``requests`` holds one row per request, so
# it can only ever name the model that answered: when a primary failed and a
# fallback succeeded the row said "success" and the reason the primary failed
# lived only in a log line. Measured over 21 days of real traffic, 1,144
# fallbacks succeeded and the largest cohort of 319 carried no recoverable
# reason at all.


def _attempt_log() -> tuple[list[RouteAttemptRecord], AttemptResultObserver]:
    seen: list[RouteAttemptRecord] = []
    return seen, seen.append


@pytest.mark.asyncio
async def test_a_rescued_request_records_why_the_primary_was_abandoned() -> None:
    """The fallback's success must not erase the primary's failure."""
    primary = FailingPreflightProvider()
    backup = FakeProvider()
    providers = {"broken": primary, "healthy": backup}
    first = _routed_request(provider_id="broken")
    second = _routed_request(provider_id="healthy")
    attempts, observer = _attempt_log()

    stream = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
    ).stream(
        _plan(first, second),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_rescued",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    assert [(a.attempt, a.model_ref, a.outcome) for a in attempts] == [
        (0, "broken/provider-model", "failed"),
        (1, "healthy/provider-model", "succeeded"),
    ]
    # The reason, which is the whole point: the request succeeded, and the log
    # still says what it had to survive to do so.
    assert attempts[0].error_kind == "ValueError"
    assert "invalid provider request" in (attempts[0].error_message or "")
    assert attempts[1].error_kind is None


@pytest.mark.asyncio
async def test_a_model_the_chain_never_reached_says_so() -> None:
    """ "Not tried" and "tried and failed" are different facts about a route."""
    provider = FakeProvider()
    attempts, observer = _attempt_log()

    stream = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _m, _s, _t: 1,
    ).stream(
        _plan(
            _routed_request(provider_id="first"),
            _routed_request(provider_id="second"),
            _routed_request(provider_id="third"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_untouched",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    assert [(a.attempt, a.outcome, a.error_message) for a in attempts] == [
        (0, "succeeded", None),
        (1, "skipped", "never reached"),
        (2, "skipped", "never reached"),
    ]


@pytest.mark.asyncio
async def test_a_model_benched_by_recent_failures_is_recorded_as_benched() -> None:
    """A three-model chain that only ran one must not look like a one-model route.

    Health ejection removes a model from the route before the request starts,
    so nothing else in the log distinguishes it from a chain that was never
    configured -- which is exactly the confusion this row exists to prevent.
    """
    health = RouteHealthRegistry(eject_after_failures=1, eject_seconds=300.0)
    health.record_failure("sick/provider-model")
    attempts, observer = _attempt_log()

    stream = ProviderExecutor(
        lambda _provider_id: FakeProvider(),
        token_counter=lambda _m, _s, _t: 1,
        health=health,
    ).stream(
        _plan(
            _routed_request(provider_id="sick"),
            _routed_request(provider_id="healthy"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_benched",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    benched = attempts[0]
    assert benched.outcome == "skipped"
    assert benched.error_kind == "ejected"
    assert "recent consecutive failures" in (benched.error_message or "")
    assert attempts[1].outcome == "succeeded"


@pytest.mark.asyncio
async def test_an_exhausted_chain_records_every_failure_not_just_the_last() -> None:
    """When everything fails, the log must name what each model did."""
    attempts, observer = _attempt_log()
    executor = ProviderExecutor(
        lambda _provider_id: FailingPreflightProvider(),
        token_counter=lambda _m, _s, _t: 1,
    )

    with pytest.raises(ValueError):
        executor.stream(
            _plan(
                _routed_request(provider_id="a"),
                _routed_request(provider_id="b"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_exhausted",
            on_attempt_result=observer,
        )

    assert [(a.attempt, a.outcome, a.error_kind) for a in attempts] == [
        (0, "failed", "ValueError"),
        (1, "failed", "ValueError"),
    ]


@pytest.mark.asyncio
async def test_an_execution_failure_is_named_by_its_kind_not_its_class() -> None:
    """One vocabulary for the attempt log.

    ``error_kind`` on the request row mixes ``FailureKind`` values with Python
    class names, which makes it awkward to group by. The attempt log prefers the
    kind wherever the failure carries one.
    """

    class RateLimited(FakeProvider):
        def preflight_stream(
            self, request: MessagesRequest, *, reasoning: ReasoningPolicy
        ) -> None:
            raise ExecutionFailure(
                kind=FailureKind.RATE_LIMIT,
                status_code=429,
                message="slow down",
                retryable=True,
            )

    attempts, observer = _attempt_log()
    providers: dict[str, ProviderPort] = {
        "limited": RateLimited(),
        "ok": FakeProvider(),
    }
    stream = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
    ).stream(
        _plan(
            _routed_request(provider_id="limited"), _routed_request(provider_id="ok")
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_kind",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    assert attempts[0].error_kind == "rate_limit"
    assert attempts[0].outcome == "failed"


@pytest.mark.asyncio
async def test_a_stream_that_dies_after_preflight_is_recorded_as_failed() -> None:
    """Preflight and streaming fail on different code paths.

    A preflight failure never opens a stream and is recorded where the chain
    picks the next candidate; a stream that dies afterwards is recorded in the
    execution loop. Covering only the first left the second free to report a
    failed attempt as a success -- verified by mutation, which the preflight
    test alone did not catch.
    """
    broken = FailingStreamConstructionProvider()
    healthy = FakeProvider()
    providers: dict[str, ProviderPort] = {"broken": broken, "healthy": healthy}
    attempts, observer = _attempt_log()

    stream = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
    ).stream(
        _plan(
            _routed_request(provider_id="broken"),
            _routed_request(provider_id="healthy"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_midstream",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    # Preflight passed, so this attempt really did start; it failed opening the
    # stream, and that is what the log has to say.
    assert broken.preflight_calls
    assert [(a.attempt, a.outcome, a.error_kind) for a in attempts] == [
        (0, "failed", "RuntimeError"),
        (1, "succeeded", None),
    ]
    assert "stream construction failed" in (attempts[0].error_message or "")
    # And the attempt that ran was timed, which is what makes a slow failure
    # legible next to a fast one. Asserted as "present", not ">= 0": the latter
    # is true of None too, and passed against a mutation that dropped timing
    # altogether.
    assert attempts[0].duration_ms is not None
    assert attempts[1].duration_ms is not None
    # A model that never ran has nothing to time.
    assert all(a.duration_ms is None for a in attempts if a.outcome == "skipped")
