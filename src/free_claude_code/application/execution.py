"""Provider execution shared by inbound API adapters."""

import sys
from collections.abc import AsyncIterator, Callable
from typing import Literal

from loguru import logger

from free_claude_code.core.anthropic import (
    Message,
    SystemContent,
    Tool,
    anthropic_request_snapshot,
    get_token_count,
)
from free_claude_code.core.credential_attribution import record_credential
from free_claude_code.core.diagnostics import safe_exception_message
from free_claude_code.core.trace import (
    close_stream_input,
    trace_event,
    traced_async_stream,
)

from .ports import ProviderPort, ProviderResolver
from .routing import RoutedMessagesPlan, RoutedMessagesRequest

TokenCounter = Callable[
    [list[Message], str | list[SystemContent] | None, list[Tool] | None],
    int,
]
WireApi = Literal["messages", "responses"]
AttemptObserver = Callable[[RoutedMessagesRequest, int], None]


class ProviderExecutor:
    """Resolve a provider and execute one routed Anthropic Messages stream."""

    def __init__(
        self,
        provider_resolver: ProviderResolver,
        *,
        token_counter: TokenCounter = get_token_count,
        generation_id: int | None = None,
        log_raw_payloads: bool = False,
    ) -> None:
        self._provider_resolver = provider_resolver
        self._token_counter = token_counter
        self._generation_id = generation_id
        self._log_raw_payloads = log_raw_payloads

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
        """
        attempts = plan.attempts
        buffer_until_complete = not plan.primary.request.stream
        failures: list[BaseException] = []
        prepared = self._prepare_from(
            attempts, 0, failures, request_id=request_id, on_attempt=on_attempt
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
            index, provider = prepared
            while True:
                routed = attempts[index]
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
                    async for chunk in provider_stream:
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
                    return

                # The failed stream is closed by now, so the next attempt never
                # runs alongside a half-open connection to the previous one.
                failures.append(uncommitted_failure)
                self._trace_fallback(
                    routed, uncommitted_failure, request_id=request_id, attempt=index
                )
                following = self._prepare_from(
                    attempts,
                    index + 1,
                    failures,
                    request_id=request_id,
                    on_attempt=on_attempt,
                )
                if following is None:
                    raise uncommitted_failure
                index, provider = following

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

    def _prepare_from(
        self,
        attempts: tuple[RoutedMessagesRequest, ...],
        start: int,
        failures: list[BaseException],
        *,
        request_id: str,
        on_attempt: AttemptObserver | None = None,
    ) -> tuple[int, ProviderPort] | None:
        """Return the first attempt at or after ``start`` that resolves and preflights.

        Preflight runs lazily, one attempt at a time, so a healthy primary never
        pays to validate a fallback it will not use.

        Every candidate is announced to ``on_attempt`` *before* it is tried, so
        the request log names the last model the chain reached even when every
        attempt fails. Announcing only the winner made an exhausted three-model
        chain indistinguishable from a primary that failed on its own.
        """
        for index in range(start, len(attempts)):
            routed = attempts[index]
            if on_attempt is not None:
                on_attempt(routed, index)
            try:
                provider = self._provider_resolver(routed.resolved.provider_id)
                provider.preflight_stream(routed.request, reasoning=routed.reasoning)
            except Exception as exc:
                failures.append(exc)
                self._trace_fallback(routed, exc, request_id=request_id, attempt=index)
                continue
            return index, provider
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
