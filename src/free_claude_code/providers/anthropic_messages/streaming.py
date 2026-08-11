"""Validate and forward native Anthropic Messages SSE frames."""

import codecs
import json
from collections.abc import AsyncIterator

from free_claude_code.providers.stream_recovery import TruncatedProviderStreamError

_TERMINAL_EVENT = "message_stop"


async def iter_anthropic_sse_frames(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Yield complete validated SSE frames and require ``message_stop``."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    terminal_seen = False
    async for chunk in chunks:
        buffer += decoder.decode(chunk).replace("\r\n", "\n")
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            frame, event_name = _validated_frame(raw)
            if frame is None:
                continue
            terminal_seen = terminal_seen or event_name == _TERMINAL_EVENT
            yield frame

    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        frame, event_name = _validated_frame(buffer)
        if frame is not None:
            terminal_seen = terminal_seen or event_name == _TERMINAL_EVENT
            yield frame
    if not terminal_seen:
        raise TruncatedProviderStreamError(
            "Anthropic Messages stream ended without message_stop."
        )


def _validated_frame(raw: str) -> tuple[str | None, str | None]:
    event_name: str | None = None
    data_parts: list[str] = []
    for line in raw.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].lstrip())

    if not data_parts:
        return None, event_name
    payload = json.loads("\n".join(data_parts))
    if not isinstance(payload, dict):
        raise ValueError("Anthropic Messages SSE data must be a JSON object.")
    payload_type = payload.get("type")
    if not isinstance(payload_type, str) or not payload_type:
        raise ValueError("Anthropic Messages SSE event is missing a type.")
    if event_name is not None and event_name != payload_type:
        raise ValueError(
            "Anthropic Messages SSE event name does not match its payload type."
        )
    normalized_event = event_name or payload_type
    return (
        f"event: {normalized_event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n",
        normalized_event,
    )
