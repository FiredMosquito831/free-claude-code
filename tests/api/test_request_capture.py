"""Tests for per-request analytics capture at the handler/stream layer."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from free_claude_code.api.request_capture import (
    RequestCapture,
    build_capture,
    extract_input_text,
    extract_request_params,
)
from free_claude_code.api.response_streams import ManagedStreamingResponse
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic.models import Message, MessagesRequest
from free_claude_code.core.async_iterators import AsyncCloseable
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    get_request_log_store,
)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _events(*frames: tuple[str, dict]) -> list[str]:
    return [f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in frames]


def _make_capture(store: RequestLogStore | None, **overrides) -> RequestCapture:
    defaults: dict[str, Any] = {
        "request_id": "req_test",
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "stream": True,
        "requested_model": "claude-sonnet-4-5",
        "input_text": "hello",
        "params": {"max_tokens": 100},
    }
    defaults.update(overrides)
    return RequestCapture(store, **defaults)


async def _collect(body: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in body]


def _final_row(store: RequestLogStore) -> dict:
    rows, total = store.list_requests()
    assert total == 1
    return rows[0]


@pytest.mark.asyncio
async def test_streaming_success_records_usage_and_text(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        for chunk in _events(
            (
                "message_start",
                {"type": "message_start", "message": {"usage": {"input_tokens": 42}}},
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello "},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "world"},
                },
            ),
            ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 7}}),
            ("message_stop", {"type": "message_stop"}),
        ):
            yield chunk

    capture = _make_capture(store)
    chunks = await _collect(capture.wrap(body()))
    store.close()

    assert len(chunks) == 5
    row = _final_row(store)
    assert row["status"] == "success"
    assert row["tokens_in"] == 42
    assert row["tokens_out"] == 7
    assert row["output_text"] == "Hello world"
    assert row["input_text"] == "hello"
    assert row["ttft_ms"] is not None
    assert row["duration_ms"] is not None
    assert row["stream"] is True


@pytest.mark.asyncio
async def test_mid_stream_failure_records_error(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(
            (
                "message_start",
                {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
            )
        )[0]
        raise ExecutionFailure(
            kind=FailureKind.RATE_LIMIT,
            status_code=429,
            message="slow down",
            retryable=True,
        )

    capture = _make_capture(store)
    with pytest.raises(ExecutionFailure):
        await _collect(capture.wrap(body()))
    store.close()

    row = _final_row(store)
    assert row["status"] == "error"
    assert row["error_kind"] == "rate_limit"
    assert row["error_message"] == "slow down"
    assert row["tokens_in"] == 3


@pytest.mark.asyncio
async def test_sse_error_event_records_error(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(
            (
                "error",
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "busy"},
                },
            )
        )[0]

    capture = _make_capture(store)
    await _collect(capture.wrap(body()))
    store.close()

    row = _final_row(store)
    assert row["status"] == "error"
    assert row["error_kind"] == "overloaded_error"


@pytest.mark.asyncio
async def test_client_disconnect_records_cancelled(store: RequestLogStore) -> None:
    closed = asyncio.Event()

    async def body() -> AsyncIterator[str]:
        try:
            yield _events(("message_start", {"type": "message_start", "message": {}}))[
                0
            ]
            await asyncio.sleep(60)
            yield "never"
        finally:
            closed.set()

    capture = _make_capture(store)
    stream = capture.wrap(body())
    await anext(stream)
    assert isinstance(stream, AsyncCloseable)
    await stream.aclose()
    store.close()

    assert closed.is_set()
    row = _final_row(store)
    assert row["status"] == "cancelled"
    assert row["ttft_ms"] is not None


@pytest.mark.asyncio
async def test_task_cancellation_records_cancelled(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(("message_start", {"type": "message_start", "message": {}}))[0]
        await asyncio.sleep(60)
        yield "never"

    capture = _make_capture(store)

    async def consume() -> None:
        async for _ in capture.wrap(body()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.close()

    row = _final_row(store)
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_pre_start_error_via_finish_error(store: RequestLogStore) -> None:
    capture = _make_capture(store)
    capture.finish_error(
        ExecutionFailure(
            kind=FailureKind.AUTHENTICATION,
            status_code=401,
            message="bad key",
            retryable=False,
        )
    )
    store.close()

    row = _final_row(store)
    assert row["status"] == "error"
    assert row["error_kind"] == "authentication"


@pytest.mark.asyncio
async def test_finish_is_single_shot(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(("message_stop", {"type": "message_stop"}))[0]

    capture = _make_capture(store)
    await _collect(capture.wrap(body()))
    capture.finish_error(RuntimeError("late error"))
    store.close()

    row = _final_row(store)
    assert row["status"] == "success"
    assert row["error_kind"] is None


@pytest.mark.asyncio
async def test_privacy_mode_stores_hashes_not_bodies(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "secret out"},
                },
            )
        )[0]

    capture = _make_capture(store, input_text="secret in", capture_bodies=False)
    await _collect(capture.wrap(body()))
    store.close()

    row = _final_row(store)
    assert row["input_text"] is None
    assert row["output_text"] is None
    assert row["input_sha256"] is not None
    assert row["output_sha256"] is not None
    assert row["input_chars"] == len("secret in")
    assert row["output_chars"] == len("secret out")


@pytest.mark.asyncio
async def test_disabled_capture_passes_stream_through() -> None:
    async def body() -> AsyncIterator[str]:
        yield "chunk"

    capture = _make_capture(None)
    assert capture.enabled is False
    assert capture.wrap(body()) is not None
    chunks = await _collect(capture.wrap(body()))
    assert chunks == ["chunk"]


def test_build_capture_from_messages_request(store: RequestLogStore) -> None:
    settings = Settings()
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        temperature=0.5,
        stream=True,
        system="be nice",
        messages=[Message(role="user", content="hi there")],
    )
    capture = build_capture(
        settings,
        request,
        request_id="req_x",
        endpoint="/v1/messages",
        protocol="anthropic",
    )
    assert capture.enabled is True
    assert extract_input_text(request) == "be nice\nhi there"
    params = extract_request_params(request)
    assert params["max_tokens"] == 100
    assert params["temperature"] == 0.5
    capture.finish_success("done")
    store_from = get_request_log_store()
    assert store_from is not None
    store_from.close()
    rows, total = store_from.list_requests()
    assert total == 1
    assert rows[0]["requested_model"] == "nvidia_nim/test-model"
    assert rows[0]["output_text"] == "done"


def test_build_capture_disabled_by_settings(monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    settings = Settings()
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        messages=[Message(role="user", content="hi")],
    )
    capture = build_capture(
        settings,
        request,
        request_id="req_x",
        endpoint="/v1/messages",
        protocol="anthropic",
    )
    assert capture.enabled is False
    capture.finish_success("done")


def test_records_exactly_once_for_non_stream_aggregate(store: RequestLogStore) -> None:
    # Simulates the non-streaming path: the same wrapped stream is consumed
    # to completion by the SSE aggregator.
    async def body() -> AsyncIterator[str]:
        yield _events(("message_stop", {"type": "message_stop"}))[0]

    capture = _make_capture(store, stream=False)
    asyncio.run(_collect(capture.wrap(body())))
    store.close()
    rows, total = store.list_requests()
    assert total == 1
    assert rows[0]["stream"] is False


def test_request_record_defaults() -> None:
    record = RequestRecord(id="r", endpoint="/v1/messages", protocol="anthropic")
    assert record.status == "success"
    assert record.ts_epoch > 0


class _FakeProvider:
    """Minimal provider stub compatible with ProviderExecutor."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def preflight_stream(self, request, *, reasoning) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset({"test-model"})

    async def stream_response(
        self,
        request,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning,
    ) -> AsyncIterator[str]:
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_messages_handler_end_to_end_capture() -> None:
    from free_claude_code.api.handlers import MessagesHandler

    events = _events(
        (
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 11}}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hi"},
            },
        ),
        ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 2}}),
        ("message_stop", {"type": "message_stop"}),
    )
    handler = MessagesHandler(
        Settings(),
        provider_resolver=lambda _: _FakeProvider(events),
    )
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=50,
        stream=True,
        messages=[Message(role="user", content="hello")],
    )
    response = await handler.create(request, request_id="req_e2e")
    assert isinstance(response, ManagedStreamingResponse)
    async for _ in response.body_iterator:
        pass
    await response.aclose()

    store = get_request_log_store()
    assert store is not None
    store.close()
    row = store.get_request("req_e2e")
    assert row is not None
    assert row["status"] == "success"
    assert row["provider"] == "nvidia_nim"
    assert row["resolved_model"] == "test-model"
    assert row["requested_model"] == "nvidia_nim/test-model"
    assert row["tokens_in"] == 11
    assert row["tokens_out"] == 2
    assert row["output_text"] == "hi"
    assert row["input_text"] == "hello"
    assert row["reasoning"] is not None
    assert row["params"]["max_tokens"] == 50
