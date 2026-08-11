"""Native Anthropic Messages upstream provider family."""

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import Any

import httpx

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.core.trace import trace_event
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.http import close_provider_stream
from my_claude_code.providers.model_listing import model_infos_from_ids
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.stream_recovery import (
    RecoveryController,
    RecoveryFailureAction,
)

from .request import build_anthropic_messages_body
from .streaming import iter_anthropic_sse_frames


class AnthropicMessagesProvider(BaseProvider):
    """Provider for upstream APIs implementing native Anthropic Messages SSE."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        provider_name: str,
        rate_limiter: ProviderRateLimiter,
    ) -> None:
        super().__init__(config)
        self._provider_name = provider_name
        self._base_url = config.base_url.rstrip("/")
        self._rate_limiter = rate_limiter
        self._client = httpx.AsyncClient(
            proxy=config.proxy or None,
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
        )

    def throttle_remaining(self) -> float:
        return self._rate_limiter.remaining_wait()

    async def cleanup(self) -> None:
        await self._client.aclose()

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset()

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        return model_infos_from_ids(await self.list_model_ids())

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        build_anthropic_messages_body(request, reasoning=reasoning)

    async def _send_stream_request(self, body: dict[str, Any]) -> httpx.Response:
        request = self._client.build_request(
            "POST",
            f"{self._base_url}/messages",
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        response = await self._client.send(request, stream=True)
        if response.status_code >= 400:
            await response.aread()
            await response.aclose()
            raise httpx.HTTPStatusError(
                f"{self._provider_name} Messages API error {response.status_code}",
                request=request,
                response=response,
            )
        return response

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        del input_tokens
        return self._stream_response(
            request,
            request_id=request_id,
            reasoning=reasoning,
        )

    async def _stream_response(
        self,
        request: MessagesRequest,
        *,
        request_id: str | None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        tag = self._provider_name
        req_tag = f" request_id={request_id}" if request_id else ""
        body = build_anthropic_messages_body(request, reasoning=reasoning)
        trace_event(
            stage="provider",
            event="provider.request.sent",
            source="provider",
            provider=tag,
            request_id=request_id,
            gateway_model=request.model,
            downstream_model=body.get("model"),
            message_count=len(body.get("messages", [])),
            tool_count=len(body.get("tools", [])),
            body={
                "model": body.get("model"),
                "message_count": len(body.get("messages", [])),
                "tool_count": len(body.get("tools", [])),
                "stream": True,
            },
        )
        recovery = RecoveryController(
            provider_name=tag,
            request_id=request_id,
            holdback_seconds=self._config.commit_holdback_seconds,
            early_retry_attempts=self._config.early_retry_attempts,
            midstream_recovery_attempts=0,
        )

        async with self._rate_limiter.concurrency_slot():
            while True:
                response: httpx.Response | None = None
                stream_opened = False
                try:
                    response = await self._rate_limiter.execute_with_retry(
                        self._send_stream_request,
                        body,
                    )
                    stream_opened = True
                    recovery.upstream_opened()
                    async for event in iter_anthropic_sse_frames(
                        response.aiter_bytes()
                    ):
                        for held_event in recovery.push(event):
                            yield held_event
                    for held_event in recovery.flush():
                        yield held_event
                    return
                except asyncio.CancelledError, GeneratorExit:
                    raise
                except Exception as error:
                    decision = recovery.advance_failure(
                        error,
                        stream_opened=stream_opened,
                        generated_output=recovery.has_buffered or recovery.committed,
                        complete_tool_salvageable=False,
                    )
                    if decision.action is RecoveryFailureAction.EARLY_RETRY:
                        continue
                    self._log_stream_transport_error(
                        tag,
                        req_tag,
                        error,
                        request_id=request_id,
                    )
                    failure = classify_provider_failure(
                        error,
                        provider_name=tag,
                        read_timeout_s=self._config.http_read_timeout,
                        request_id=request_id,
                        mark_rate_limited=self._rate_limiter.extend_reactive_block,
                        cooldown_seconds=self._config.rate_limit_cooldown_seconds,
                    )
                    trace_event(
                        stage="provider",
                        event="provider.response.error",
                        source="provider",
                        provider=tag,
                        request_id=request_id,
                        exc_type=type(error).__name__,
                        failure_kind=failure.kind.value,
                        status_code=failure.status_code,
                        provider_retryable=failure.retryable,
                    )
                    if not decision.committed:
                        recovery.discard()
                    raise failure from error
                finally:
                    if response is not None:
                        await close_provider_stream(
                            response,
                            active_error=sys.exception(),
                            provider_name=tag,
                            request_id=request_id,
                        )
