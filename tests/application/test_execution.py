"""Application-owned provider execution contracts."""

from collections.abc import AsyncIterator, Mapping
from unittest.mock import MagicMock

import pytest

from free_claude_code.application.execution import ProviderExecutor
from free_claude_code.application.ports import ProviderPort
from free_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from free_claude_code.config.reasoning import ReasoningPreference
from free_claude_code.core.anthropic.models import Message, MessagesRequest
from free_claude_code.core.async_iterators import AsyncCloseable
from free_claude_code.core.reasoning import ReasoningPolicy


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
