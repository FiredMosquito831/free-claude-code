"""Classify native Anthropic SSE error payloads into neutral failures."""

from collections.abc import Mapping

from free_claude_code.core.failures import ExecutionFailure, FailureKind


def anthropic_stream_failure(payload: Mapping[str, object] | None) -> ExecutionFailure:
    """Translate one native Anthropic error event without provider dependencies."""
    error = payload.get("error") if payload is not None else None
    error_type = error.get("type") if isinstance(error, Mapping) else None
    message = error.get("message") if isinstance(error, Mapping) else None
    safe_message = message if isinstance(message, str) else "Provider stream failed."
    kind, status, retryable = {
        "authentication_error": (FailureKind.AUTHENTICATION, 401, False),
        "permission_error": (FailureKind.PERMISSION, 403, False),
        "invalid_request_error": (FailureKind.INVALID_REQUEST, 400, False),
        "rate_limit_error": (FailureKind.RATE_LIMIT, 429, True),
        "overloaded_error": (FailureKind.OVERLOADED, 529, True),
    }.get(error_type, (FailureKind.UPSTREAM, 502, True))
    return ExecutionFailure(
        kind=kind,
        status_code=status,
        message=safe_message,
        retryable=retryable,
    )
