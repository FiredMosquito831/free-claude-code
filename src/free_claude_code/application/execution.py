"""Provider execution shared by inbound API adapters."""

import asyncio
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from free_claude_code.config.constants import (
    FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT,
    FALLBACK_TOTAL_TIMEOUT_DEFAULT,
)
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic import (
    Message,
    SystemContent,
    Tool,
    anthropic_request_snapshot,
    get_token_count,
)
from free_claude_code.core.credential_attribution import record_credential
from free_claude_code.core.diagnostics import safe_exception_message
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.trace import (
    close_stream_input,
    trace_event,
    traced_async_stream,
)

from .ports import ProviderPort, ProviderResolver
from .route_health import RouteHealthRegistry
from .routing import RoutedMessagesPlan, RoutedMessagesRequest

TokenCounter = Callable[
    [list[Message], str | list[SystemContent] | None, list[Tool] | None],
    int,
]
WireApi = Literal["messages", "responses"]
AttemptObserver = Callable[[RoutedMessagesRequest, int], None]


@dataclass(frozen=True, slots=True)
class RouteExecutionPolicy:
    """Wall-clock limits deciding when an attempt stops being worth waiting for.

    A provider that accepts a request and then produces nothing is
    indistinguishable, to a caller with no deadline, from one that is thinking
    hard. The only thing that ever ended such an attempt was the transport read
    timeout -- minutes later, after which the stream was retried against the
    same stalled model. Both numbers below exist so a stall is declared while
    the chain can still do something about it.

    ``first_token_timeout`` is the one that matters for fallback: nothing has
    reached the client before the first chunk, so abandoning the attempt there
    is invisible and the next model simply answers instead.

    ``total_timeout`` is a backstop for the case no chain can rescue -- a stream
    that committed and then stalled. It cannot fall back, but it can stop.

    Either value at or below zero disables that limit.
    """

    first_token_timeout: float = FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT
    total_timeout: float = FALLBACK_TOTAL_TIMEOUT_DEFAULT


class _DeadlineExceeded(Exception):
    """Internal marker: our own wait elapsed, not an upstream timeout."""


async def _next_chunk(chunks: AsyncIterator[str], timeout: float | None) -> str:
    """Return the next chunk, or raise ``_DeadlineExceeded`` if ``timeout`` elapses.

    ``asyncio.wait`` rather than ``wait_for`` so a ``TimeoutError`` raised *by
    the provider* stays distinguishable from this deadline elapsing. The two
    mean different things -- one is the upstream giving up, the other is us
    deciding not to keep waiting -- and only the second should be reported as a
    routing deadline.
    """
    if timeout is None:
        return await anext(chunks)
    pending = asyncio.ensure_future(anext(chunks))
    done, _still_running = await asyncio.wait({pending}, timeout=timeout)
    if not done:
        pending.cancel()
        # Let the cancellation land before the caller closes the stream, so a
        # half-cancelled task cannot outlive the attempt that owns it.
        await asyncio.gather(pending, return_exceptions=True)
        raise _DeadlineExceeded
    return pending.result()


def _timeout_failure(
    model_ref: str, *, seconds: float, first_token: bool
) -> ExecutionFailure:
    reason = (
        f"produced no output within {seconds:g}s"
        if first_token
        else f"exceeded the {seconds:g}s request budget"
    )
    return ExecutionFailure(
        kind=FailureKind.TIMEOUT,
        status_code=504,
        message=f"Provider '{model_ref}' {reason}.",
        retryable=True,
    )


class ProviderExecutor:
    """Resolve a provider and execute one routed Anthropic Messages stream."""

    def __init__(
        self,
        provider_resolver: ProviderResolver,
        *,
        token_counter: TokenCounter = get_token_count,
        generation_id: int | None = None,
        log_raw_payloads: bool = False,
        policy: RouteExecutionPolicy | None = None,
        health: RouteHealthRegistry | None = None,
    ) -> None:
        self._provider_resolver = provider_resolver
        self._token_counter = token_counter
        self._generation_id = generation_id
        self._log_raw_payloads = log_raw_payloads
        self._policy = policy or RouteExecutionPolicy()
        self._health = health or RouteHealthRegistry()

    def stream(
        self,
        plan: RoutedMessagesPlan,
        *,
        wire_api: WireApi,
        raw_log_label: str,
        raw_log_payload: object,
        request_id: str,
        on_attempt: AttemptObserver | None = None,
    ) -> AsyncIterator[str]:
        """Preflight synchronously, then return the traced provider stream.

        Attempts are tried in order until one commits to the wire; past that
        point a failure propagates instead of moving to the next model, because
        swapping models mid-stream would splice two different completions into
        one answer.

        What "committed" means depends on the client. A streaming client sees
        each chunk as it is produced, so the first chunk commits. A
        non-streaming client is served one aggregated JSON message and sees
        nothing until the stream ends -- so nothing is committed until the
        attempt completes, and a failure at any point can still fall back to
        the next model with the client none the wiser.

        A model that has just failed repeatedly is skipped outright, so a
        request does not re-pay its timeout on the way to a healthy fallback.
        """
        attempts = plan.attempts
        buffer_until_complete = not plan.primary.request.stream
        failures: list[BaseException] = []
        order = self._health.usable_indexes(plan.model_refs())
        if len(order) < len(attempts):
            logger.info(
                "MODEL CHAIN: skipping {} recently-failing model(s) on this route",
                len(attempts) - len(order),
            )
        deadline = (
            time.monotonic() + self._policy.total_timeout
            if self._policy.total_timeout > 0
            else None
        )
        prepared = self._prepare_from(
            attempts,
            order,
            0,
            failures,
            request_id=request_id,
            on_attempt=on_attempt,
            deadline=deadline,
        )
        if prepared is None:
            # Every attempt failed before opening a stream. Raising here keeps
            # the caller's existing synchronous error surface intact.
            raise failures[-1]

        trace_event(
            stage="ingress",
            event=(
                "free_claude_code.api.responses.request.received"
                if wire_api == "responses"
                else "free_claude_code.api.request.received"
            ),
            source="api",
            message_count=len(plan.primary.request.messages),
            snapshot=anthropic_request_snapshot(plan.primary.request),
            request_id=request_id,
        )

        if self._log_raw_payloads:
            logger.debug(f"{raw_log_label} [{{}}]: {{}}", request_id, raw_log_payload)

        input_tokens = self._token_counter(
            plan.primary.request.messages,
            plan.primary.request.system,
            plan.primary.request.tools,
        )

        async def provider_body() -> AsyncIterator[str]:
            position, provider = prepared
            while True:
                index = order[position]
                routed = attempts[index]
                model_ref = routed.resolved.provider_model_ref
                self._trace_route(
                    routed,
                    wire_api=wire_api,
                    request_id=request_id,
                    attempt=index,
                    attempt_count=len(attempts),
                )

                provider_stream: AsyncIterator[str] | None = None
                committed = False
                uncommitted_failure: Exception | None = None
                held: list[str] = []
                try:
                    # Baseline attribution for single-credential providers. A
                    # rotating provider overwrites this with the credential it
                    # actually picks for this request.
                    record_credential(0, provider.credential_label)
                    provider_stream = provider.stream_response(
                        routed.request,
                        input_tokens=input_tokens,
                        request_id=request_id,
                        reasoning=routed.reasoning,
                    )
                    chunks = provider_stream.__aiter__()
                    seen_chunk = False
                    while True:
                        try:
                            chunk = await _next_chunk(
                                chunks, self._chunk_timeout(seen_chunk, deadline)
                            )
                        except StopAsyncIteration:
                            break
                        except _DeadlineExceeded as exc:
                            raise self._deadline_reached(
                                model_ref,
                                seen_chunk=seen_chunk,
                                request_id=request_id,
                            ) from exc
                        seen_chunk = True
                        if buffer_until_complete:
                            held.append(chunk)
                            continue
                        committed = True
                        yield chunk
                except Exception as exc:
                    if committed:
                        raise
                    uncommitted_failure = exc
                finally:
                    if provider_stream is not None:
                        await close_stream_input(
                            provider_stream,
                            owner="provider_executor",
                            source="api",
                            preserved_error=sys.exception(),
                        )
                if uncommitted_failure is None:
                    # Empty unless this attempt was held back for a
                    # non-streaming client; a failed attempt's chunks are
                    # dropped with it and never reach the aggregator.
                    for chunk in held:
                        yield chunk
                    self._health.record_success(model_ref)
                    return

                # The failed stream is closed by now, so the next attempt never
                # runs alongside a half-open connection to the previous one.
                failures.append(uncommitted_failure)
                self._health.record_failure(model_ref)
                self._trace_fallback(
                    routed, uncommitted_failure, request_id=request_id, attempt=index
                )
                following = self._prepare_from(
                    attempts,
                    order,
                    position + 1,
                    failures,
                    request_id=request_id,
                    on_attempt=on_attempt,
                    deadline=deadline,
                )
                if following is None:
                    raise uncommitted_failure
                position, provider = following

        stream_trace: dict[str, object] = {
            "request_id": request_id,
            "provider_id": plan.primary.resolved.provider_id,
            "gateway_model": plan.primary.request.model,
        }
        if self._generation_id is not None:
            stream_trace["generation_id"] = self._generation_id

        return traced_async_stream(
            provider_body(),
            stage="egress",
            source="api",
            complete_event=(
                "free_claude_code.api.responses.stream_completed"
                if wire_api == "responses"
                else "free_claude_code.api.response.stream_completed"
            ),
            interrupted_event=(
                "free_claude_code.api.responses.stream_interrupted"
                if wire_api == "responses"
                else "free_claude_code.api.response.stream_interrupted"
            ),
            chunk_event=None,
            extra=stream_trace,
        )

    def _chunk_timeout(self, seen_chunk: bool, deadline: float | None) -> float | None:
        """Seconds to wait for the next chunk, or ``None`` to wait indefinitely.

        The first-token limit applies once, to the wait preceding the first
        chunk; the total budget applies to every wait, so a stream that stalls
        after committing still ends at the budget rather than at the transport
        read timeout.
        """
        limits: list[float] = []
        if not seen_chunk and self._policy.first_token_timeout > 0:
            limits.append(self._policy.first_token_timeout)
        if deadline is not None:
            limits.append(max(0.0, deadline - time.monotonic()))
        return min(limits) if limits else None

    def _deadline_reached(
        self, model_ref: str, *, seen_chunk: bool, request_id: str
    ) -> ExecutionFailure:
        first_token = not seen_chunk
        seconds = (
            self._policy.first_token_timeout
            if first_token
            else self._policy.total_timeout
        )
        logger.warning(
            "MODEL DEADLINE: '{}' {} after {:g}s",
            model_ref,
            "produced no first token" if first_token else "exceeded the request budget",
            seconds,
        )
        trace_event(
            stage="routing",
            event="free_claude_code.api.route.deadline",
            source="api",
            request_id=request_id,
            provider_model_ref=model_ref,
            first_token=first_token,
            timeout_seconds=seconds,
        )
        return _timeout_failure(model_ref, seconds=seconds, first_token=first_token)

    def _prepare_from(
        self,
        attempts: tuple[RoutedMessagesRequest, ...],
        order: tuple[int, ...],
        start: int,
        failures: list[BaseException],
        *,
        request_id: str,
        on_attempt: AttemptObserver | None = None,
        deadline: float | None = None,
    ) -> tuple[int, ProviderPort] | None:
        """Return the first attempt at or after ``start`` that resolves and preflights.

        Preflight runs lazily, one attempt at a time, so a healthy primary never
        pays to validate a fallback it will not use.

        Every candidate is announced to ``on_attempt`` *before* it is tried, so
        the request log names the last model the chain reached even when every
        attempt fails. Announcing only the winner made an exhausted three-model
        chain indistinguishable from a primary that failed on its own.

        ``start`` indexes ``order``, not ``attempts``: a model benched by recent
        failures is not in ``order`` at all and is never reached.
        """
        for position in range(start, len(order)):
            index = order[position]
            routed = attempts[index]
            model_ref = routed.resolved.provider_model_ref
            if deadline is not None and time.monotonic() >= deadline:
                # Starting another model with no time left only delays the
                # error the caller is already going to see.
                logger.warning(
                    "MODEL CHAIN EXHAUSTED: request budget spent before trying '{}'",
                    model_ref,
                )
                failures.append(
                    _timeout_failure(
                        model_ref,
                        seconds=self._policy.total_timeout,
                        first_token=False,
                    )
                )
                return None
            if on_attempt is not None:
                on_attempt(routed, index)
            try:
                provider = self._provider_resolver(routed.resolved.provider_id)
                provider.preflight_stream(routed.request, reasoning=routed.reasoning)
            except Exception as exc:
                failures.append(exc)
                self._health.record_failure(model_ref)
                self._trace_fallback(routed, exc, request_id=request_id, attempt=index)
                continue
            return position, provider
        return None

    def _trace_route(
        self,
        routed: RoutedMessagesRequest,
        *,
        wire_api: WireApi,
        request_id: str,
        attempt: int,
        attempt_count: int,
    ) -> None:
        route_trace: dict[str, object] = {
            "stage": "routing",
            "event": "free_claude_code.api.route.resolved",
            "source": "api",
            "request_id": request_id,
            "provider_id": routed.resolved.provider_id,
            "provider_model": routed.resolved.provider_model,
            "provider_model_ref": routed.resolved.provider_model_ref,
            "gateway_model": routed.request.model,
            "reasoning_control": routed.reasoning.control.value,
            "reasoning_effort": (
                routed.reasoning.effort.value
                if routed.reasoning.effort is not None
                else None
            ),
            "reasoning_budget_tokens": routed.reasoning.budget_tokens,
        }
        if attempt_count > 1:
            route_trace["attempt"] = attempt
            route_trace["attempt_count"] = attempt_count
        if wire_api == "responses":
            route_trace["wire_api"] = "responses"
        if self._generation_id is not None:
            route_trace["generation_id"] = self._generation_id
        trace_event(**route_trace)

    def _trace_fallback(
        self,
        routed: RoutedMessagesRequest,
        exc: BaseException,
        *,
        request_id: str,
        attempt: int,
    ) -> None:
        reason = safe_exception_message(exc)
        logger.warning(
            "MODEL FALLBACK: attempt {} '{}' failed before streaming: {}",
            attempt,
            routed.resolved.provider_model_ref,
            reason,
        )
        trace_event(
            stage="routing",
            event="free_claude_code.api.route.fallback",
            source="api",
            request_id=request_id,
            attempt=attempt,
            provider_id=routed.resolved.provider_id,
            provider_model_ref=routed.resolved.provider_model_ref,
            error_kind=type(exc).__name__,
            reason=reason,
        )


def route_execution_policy(settings: Settings) -> RouteExecutionPolicy:
    """Read the route deadlines a request should run under."""
    return RouteExecutionPolicy(
        first_token_timeout=settings.fallback_first_token_timeout,
        total_timeout=settings.fallback_total_timeout,
    )


def route_health_registry(settings: Settings) -> RouteHealthRegistry:
    """Build an ejection registry from settings.

    One registry per executor, so what a route learned about a model outlives a
    single request. A registry rebuilt per request would never reach its
    failure threshold and would eject nothing.
    """
    return RouteHealthRegistry(
        eject_after_failures=settings.fallback_eject_after_failures,
        eject_seconds=settings.fallback_eject_seconds,
    )
