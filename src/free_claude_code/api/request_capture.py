"""Per-request analytics capture at the handler/stream layer.

One :class:`RequestCapture` per request accumulates routing metadata, output
text, usage and timing from the Anthropic SSE stream, then enqueues exactly
one :class:`RequestRecord` into the request log store when the request
terminates (success, error, or client cancellation).
"""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

from free_claude_code.application.routing import RoutedMessagesRequest
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic import MessagesRequest
from free_claude_code.core.async_iterators import try_close_async_iterator
from free_claude_code.core.credential_attribution import install_attribution
from free_claude_code.core.diagnostics import safe_exception_message
from free_claude_code.core.failures import find_execution_failure
from free_claude_code.core.request_log import (
    MAX_TEXT_CHARS,
    RequestLogStore,
    RequestRecord,
    store_from_settings,
)

WireProtocol = Literal["anthropic", "openai_responses"]


class RequestCapture:
    """Accumulate one request's analytics and emit exactly one log record."""

    def __init__(
        self,
        store: RequestLogStore | None,
        *,
        request_id: str,
        endpoint: str,
        protocol: WireProtocol,
        stream: bool,
        requested_model: str | None,
        input_text: str | None,
        params: dict[str, Any] | None,
        capture_bodies: bool = True,
    ) -> None:
        self._store = store
        self._capture_bodies = capture_bodies
        self._start = time.perf_counter()
        self._ttft_ms: float | None = None
        self._output_parts: list[str] = []
        self._output_chars = 0
        self._stored_chars = 0
        self._tokens_in: int | None = None
        self._cache_read_tokens: int | None = None
        self._cache_write_tokens: int | None = None
        self._tokens_out: int | None = None
        self._error: tuple[str | None, str | None] | None = None
        self._finalized = False
        # The rotating provider writes the credential it picks into this slot
        # from deep in the call stack; it is read back at finalize time.
        self._credential = install_attribution()
        input_chars = len(input_text) if input_text else None
        self._record = RequestRecord(
            id=request_id,
            endpoint=endpoint,
            protocol=protocol,
            stream=stream,
            requested_model=requested_model,
            input_text=input_text if capture_bodies else None,
            input_sha256=(
                None if input_text is None or capture_bodies else _sha256(input_text)
            ),
            input_chars=input_chars,
            params=params,
        )

    @property
    def enabled(self) -> bool:
        return self._store is not None

    def set_routing(self, routed: RoutedMessagesRequest) -> None:
        """Attach provider/model/reasoning metadata once routing resolves."""
        if not self.enabled:
            return
        self._record.provider = routed.resolved.provider_id
        self._record.resolved_model = routed.resolved.provider_model
        self._record.reasoning = _describe_reasoning(routed)

    def finish_error(self, exc: BaseException) -> None:
        """Finalize for an error raised before the stream wrapper takes over."""
        failure = find_execution_failure(exc)
        if failure is not None:
            self._error = (failure.kind.value, failure.message)
        else:
            self._error = (type(exc).__name__, safe_exception_message(exc))
        self._finalize("error")

    def finish_success(self, output_text: str | None = None) -> None:
        """Finalize a non-streamed (short-circuited) successful response."""
        if output_text:
            self._append_output(output_text)
        self._finalize("success")

    def wrap(self, body: AsyncIterator[str]) -> AsyncIterator[str]:
        """Wrap the Anthropic SSE stream, observing every chunk pass through."""
        if not self.enabled:
            return body
        return self._observe(body)

    async def _observe(self, body: AsyncIterator[str]) -> AsyncIterator[str]:
        buffer = ""
        status: Literal["success", "error", "cancelled"] = "success"
        saw_chunk = False
        try:
            async for chunk in body:
                if self._ttft_ms is None:
                    self._ttft_ms = (time.perf_counter() - self._start) * 1000
                saw_chunk = True
                buffer = self._consume_buffer(buffer + chunk)
                yield chunk
            if self._error is not None:
                status = "error"
            elif not saw_chunk:
                self._error = ("empty_stream", "Stream ended before any content.")
                status = "error"
        except GeneratorExit:
            status = "cancelled"
            self._finalize(status)
            await try_close_async_iterator(body)
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            self._finalize(status)
            raise
        except BaseException as exc:
            failure = find_execution_failure(exc)
            if failure is not None:
                self._error = (failure.kind.value, failure.message)
            else:
                self._error = (type(exc).__name__, safe_exception_message(exc))
            status = "error"
            raise
        finally:
            if status != "cancelled":
                self._finalize(status)

    def _consume_buffer(self, buffer: str) -> str:
        """Parse complete SSE frames from the buffer; return the remainder."""
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            self._parse_frame(frame)
        return buffer

    def _parse_frame(self, frame: str) -> None:
        data_lines: list[str] = [
            line[len("data:") :].strip()
            for line in frame.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            return
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        if event_type == "message_start":
            message = payload.get("message")
            if isinstance(message, dict):
                usage = message.get("usage")
                if isinstance(usage, dict):
                    self._tokens_in = _int_or_none(usage.get("input_tokens"))
                    self._read_cache_usage(usage)
        elif event_type == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str):
                    self._append_output(text)
        elif event_type == "message_delta":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                output_tokens = _int_or_none(usage.get("output_tokens"))
                if output_tokens is not None:
                    self._tokens_out = output_tokens
                # Anthropic-native upstreams report cache counters up front on
                # message_start, but everything translated from an OpenAI-shaped
                # provider only learns them from the final usage chunk, so they
                # arrive here. Reading both is what makes the figure appear for
                # OpenRouter, DeepSeek and the rest.
                self._read_cache_usage(usage)
        elif event_type == "error":
            error = payload.get("error")
            if isinstance(error, dict):
                kind = error.get("type")
                message = error.get("message")
                self._error = (
                    kind if isinstance(kind, str) else "api_error",
                    message if isinstance(message, str) else "Stream error.",
                )

    def _read_cache_usage(self, usage: dict[str, object]) -> None:
        """Record cache counters from whichever usage payload carries them."""

        cache_read = _int_or_none(usage.get("cache_read_input_tokens"))
        if cache_read is not None:
            self._cache_read_tokens = cache_read
        cache_write = _int_or_none(usage.get("cache_creation_input_tokens"))
        if cache_write is not None:
            self._cache_write_tokens = cache_write

    def _append_output(self, text: str) -> None:
        self._output_chars += len(text)
        remaining = MAX_TEXT_CHARS - self._stored_chars
        if remaining > 0:
            self._output_parts.append(text[:remaining])
            self._stored_chars += min(remaining, len(text))

    def _finalize(self, status: Literal["success", "error", "cancelled"]) -> None:
        if self._finalized or self._store is None:
            self._finalized = True
            return
        self._finalized = True
        record = self._record
        record.status = status
        record.duration_ms = (time.perf_counter() - self._start) * 1000
        record.ttft_ms = self._ttft_ms
        record.tokens_in = self._tokens_in
        record.tokens_out = self._tokens_out
        record.cache_read_tokens = self._cache_read_tokens
        record.cache_write_tokens = self._cache_write_tokens
        output_text = "".join(self._output_parts)
        record.output_chars = self._output_chars
        if self._capture_bodies:
            record.output_text = output_text or None
        elif output_text:
            record.output_sha256 = _sha256(output_text)
        if self._error is not None:
            record.error_kind, record.error_message = self._error
        record.key_index = self._credential.index
        record.key_label = self._credential.label
        self._store.enqueue(record)


def build_capture(
    settings: Settings,
    request: MessagesRequest,
    *,
    request_id: str,
    endpoint: str,
    protocol: WireProtocol,
) -> RequestCapture:
    """Create the capture for one request; inert when logging is disabled."""
    store = store_from_settings(settings)
    return RequestCapture(
        store,
        request_id=request_id,
        endpoint=endpoint,
        protocol=protocol,
        stream=bool(request.stream),
        requested_model=request.model,
        input_text=extract_input_text(request),
        params=extract_request_params(request),
        capture_bodies=bool(getattr(settings, "request_log_capture_bodies", True)),
    )


def extract_input_text(request: MessagesRequest) -> str | None:
    """Concatenate system and message text for the request log."""
    parts: list[str] = []
    system = request.system
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        for block in system:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    for message in request.messages:
        content = message.content
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
    joined = "\n".join(part for part in parts if part)
    return joined or None


def extract_request_params(request: MessagesRequest) -> dict[str, Any]:
    """Snapshot non-credential request parameters for the request log."""
    params: dict[str, Any] = {
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "top_k": request.top_k,
        "stop_sequences": request.stop_sequences,
        "tools_count": len(request.tools) if request.tools else 0,
        "tool_choice": request.tool_choice,
        "thinking": (
            request.thinking.model_dump(mode="json", exclude_none=True)
            if request.thinking is not None
            else None
        ),
    }
    return {key: value for key, value in params.items() if value is not None}


def _describe_reasoning(routed: RoutedMessagesRequest) -> str | None:
    policy = routed.reasoning
    parts = [f"control={policy.control.value}"]
    if policy.effort is not None:
        parts.append(f"effort={policy.effort.value}")
    if policy.budget_tokens is not None:
        parts.append(f"budget={policy.budget_tokens}")
    return ",".join(parts)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def extract_output_text_from_message(message: Any) -> str | None:
    """Extract assistant text from a complete Anthropic message payload."""
    model_dump = getattr(message, "model_dump", None)
    if callable(model_dump):
        message = model_dump(mode="json")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts) or None


__all__ = [
    "RequestCapture",
    "build_capture",
    "extract_input_text",
    "extract_output_text_from_message",
    "extract_request_params",
]
