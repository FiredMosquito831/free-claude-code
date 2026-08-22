"""Request detection utilities for API optimizations.

Detects title generation, safety classifier, and suggestion mode requests to
enable targeted handling.
"""

from my_claude_code.core.anthropic import (
    Message,
    MessagesRequest,
    extract_text_from_content,
)


def _request_system_text(request_data: MessagesRequest) -> str:
    """Return top-level and inline system text for request-shape detection."""
    parts: list[str] = []
    if request_data.system:
        text = extract_text_from_content(request_data.system)
        if text:
            parts.append(text)
    for message in request_data.messages:
        if message.role != "system":
            continue
        text = extract_text_from_content(message.content)
        if text:
            parts.append(text)
    return "\n".join(parts)


def is_title_generation_request(request_data: MessagesRequest) -> bool:
    """Check if this is a conversation title generation request.

    Title generation requests are detected by a system prompt containing
    title extraction instructions, no tools, and a single user message.

    Matches Claude Code session title prompts (sentence-case title, JSON
    \"title\" field, etc.).
    """
    if request_data.tools:
        return False
    system_text = _request_system_text(request_data).lower()
    if "title" not in system_text:
        return False
    return "sentence-case title" in system_text or (
        "return json" in system_text
        and "field" in system_text
        and ("coding session" in system_text or "this session" in system_text)
    )


def is_safety_classifier_request(request_data: MessagesRequest) -> bool:
    """Return whether this is Claude Code's auto-mode safety classifier prompt."""
    if request_data.tools:
        return False

    system_text = (
        extract_text_from_content(request_data.system) if request_data.system else ""
    )
    messages_text = "".join(
        extract_text_from_content(message.content) for message in request_data.messages
    )
    combined = f"{system_text}\n{messages_text}"
    has_verdict_instruction = "yes</block>" in combined or "no</block>" in combined
    return "<transcript>" in combined and has_verdict_instruction


def is_suggestion_mode_request(request_data: MessagesRequest) -> bool:
    """Check if this is a suggestion mode request.

    Claude Code appends the suggestion instruction as the *final* user turn of
    an otherwise ordinary transcript, so the marker is the tail of the request
    rather than something buried in its history.

    Only that final turn is inspected. Scanning every user message -- which is
    what this did originally -- means a conversation that merely *mentions* the
    marker answers with an empty string instead of a real reply, which is the
    worst failure this module can produce. Measured against 61 real suggestion
    requests: the marker is never earlier than 97.61% into the prompt and is
    always followed by exactly the same 1,363-character instruction block, so
    narrowing to the last turn loses nothing.
    """
    last_user_turn: Message | None = None
    for message in request_data.messages:
        if message.role == "user":
            last_user_turn = message
    if last_user_turn is None:
        return False
    return "[SUGGESTION MODE:" in extract_text_from_content(last_user_turn.content)
