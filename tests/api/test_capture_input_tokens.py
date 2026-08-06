"""What the request log stores for input tokens.

``message_start`` carries a pre-flight estimate because the upstream has not
reported anything yet; the real count arrives on ``message_delta``. Keeping the
estimate produced rows where the cached tokens exceeded the entire input.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from free_claude_code.api.request_capture import RequestCapture
from free_claude_code.core.request_log import RequestLogStore


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _events(*payloads: dict[str, Any]) -> list[str]:
    return [
        f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"
        for payload in payloads
    ]


async def _run(capture: RequestCapture, frames: list[str]) -> None:
    async def body() -> AsyncIterator[str]:
        for frame in frames:
            yield frame

    async for _ in capture.wrap(body()):
        pass


def _capture(store: RequestLogStore) -> RequestCapture:
    return RequestCapture(
        store,
        request_id="req_test",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="m",
        input_text="hello",
        params=None,
    )


def _row(store: RequestLogStore) -> dict[str, Any]:
    row = store.get_request("req_test")
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_provider_count_supersedes_the_estimate(store: RequestLogStore) -> None:
    frames = _events(
        {"type": "message_start", "message": {"usage": {"input_tokens": 12000}}},
        {
            "type": "message_delta",
            "usage": {
                "input_tokens": 6848,
                "output_tokens": 163,
                "cache_read_input_tokens": 261120,
            },
        },
    )
    capture = _capture(store)
    await _run(capture, frames)
    store.close()

    row = _row(store)
    assert row["tokens_in"] == 6848
    assert row["cache_read_tokens"] == 261120
    # The real prompt was 267,968 tokens, nearly all of it served from cache.
    assert row["tokens_in"] + row["cache_read_tokens"] == 267968


@pytest.mark.asyncio
async def test_estimate_is_kept_when_upstream_stays_silent(
    store: RequestLogStore,
) -> None:
    """A stream that never reports usage still gets the pre-flight figure."""
    frames = _events(
        {"type": "message_start", "message": {"usage": {"input_tokens": 12000}}},
        {"type": "message_delta", "usage": {"output_tokens": 7}},
    )
    capture = _capture(store)
    await _run(capture, frames)
    store.close()

    assert _row(store)["tokens_in"] == 12000


@pytest.mark.asyncio
async def test_cached_never_exceeds_the_recorded_input(
    store: RequestLogStore,
) -> None:
    """The signature of the bug: 608 stored rows had cache_read > tokens_in."""
    frames = _events(
        {"type": "message_start", "message": {"usage": {"input_tokens": 40}}},
        {
            "type": "message_delta",
            "usage": {
                "input_tokens": 2048,
                "output_tokens": 9,
                "cache_read_input_tokens": 1920,
            },
        },
    )
    capture = _capture(store)
    await _run(capture, frames)
    store.close()

    row = _row(store)
    assert row["cache_read_tokens"] <= row["tokens_in"] + row["cache_read_tokens"]
    assert row["tokens_in"] == 2048


@pytest.mark.asyncio
async def test_cache_write_is_stored(store: RequestLogStore) -> None:
    frames = _events(
        {
            "type": "message_delta",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 9,
                "cache_creation_input_tokens": 4096,
            },
        },
    )
    capture = _capture(store)
    await _run(capture, frames)
    store.close()

    assert _row(store)["cache_write_tokens"] == 4096
