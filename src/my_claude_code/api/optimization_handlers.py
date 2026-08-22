"""Optimization handlers for fast-path API responses.

Each handler returns a :class:`LocalOptimization` if the request matches and
the optimization is enabled, otherwise None. A match means the proxy answers
the request itself and no provider is contacted at all, so every match is
recorded against the rule that produced it -- a rule nobody can count is a
rule nobody can evaluate.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

from my_claude_code.application.execution import TokenCounter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    MessagesRequest,
    MessagesResponse,
    Usage,
    count_text_tokens,
)

from .detection import (
    is_suggestion_mode_request,
    is_title_generation_request,
)


@dataclass(frozen=True, slots=True)
class LocalOptimization:
    """A request answered inside the proxy instead of by a model.

    ``tokens_saved`` is the request's own input token count -- the tokens that
    would have gone upstream had the rule not matched. It is a measurement of
    this request, not an estimate of a bill: what the provider would have
    charged for the reply is unknowable and deliberately not guessed at.
    """

    rule: str
    response: MessagesResponse
    tokens_saved: int


def _answer(
    request_data: MessagesRequest,
    text: str,
    *,
    rule: str,
    token_counter: TokenCounter,
) -> LocalOptimization:
    """Build the local reply, with usage counted rather than invented.

    The counts this reports used to be hardcoded (``input_tokens=100``,
    ``output_tokens=5``) regardless of the request. They reach the client and
    feed its own accounting, so they are measured now: cl100k over the real
    request costs 0.5 ms at 1.5 KB and 7 ms at the median title prompt,
    against the multi-second upstream round trip the match avoids.
    """
    input_tokens = token_counter(
        request_data.messages, request_data.system, request_data.tools
    )
    output_tokens = count_text_tokens(text)
    logger.info("Optimization: {} answered locally", rule)
    return LocalOptimization(
        rule=rule,
        response=MessagesResponse(
            id=f"msg_{uuid.uuid4()}",
            model=request_data.model,
            content=[{"type": "text", "text": text}],
            stop_reason="end_turn",
            usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        ),
        tokens_saved=input_tokens,
    )


def try_title_skip(
    request_data: MessagesRequest, settings: Settings, token_counter: TokenCounter
) -> LocalOptimization | None:
    """Skip title generation requests."""
    if not settings.enable_title_generation_skip:
        return None
    if not is_title_generation_request(request_data):
        return None

    return _answer(
        request_data,
        "Conversation",
        rule="title_generation_skip",
        token_counter=token_counter,
    )


def try_suggestion_skip(
    request_data: MessagesRequest, settings: Settings, token_counter: TokenCounter
) -> LocalOptimization | None:
    """Skip suggestion mode requests."""
    if not settings.enable_suggestion_mode_skip:
        return None
    if not is_suggestion_mode_request(request_data):
        return None

    return _answer(
        request_data,
        "",
        rule="suggestion_mode_skip",
        token_counter=token_counter,
    )


OptimizationHandler = Callable[
    [MessagesRequest, Settings, TokenCounter], LocalOptimization | None
]

# Cheapest/most common optimizations first for faster short-circuit.
OPTIMIZATION_HANDLERS: list[OptimizationHandler] = [
    try_title_skip,
    try_suggestion_skip,
]

# Every rule name this module can record, so a consumer can enumerate them
# without importing the handlers or scraping strings out of the log.
OPTIMIZATION_RULES: tuple[str, ...] = (
    "title_generation_skip",
    "suggestion_mode_skip",
)


def try_optimizations(
    request_data: MessagesRequest, settings: Settings, token_counter: TokenCounter
) -> LocalOptimization | None:
    """Run optimization handlers in order. Returns first match or None."""
    for handler in OPTIMIZATION_HANDLERS:
        result = handler(request_data, settings, token_counter)
        if result is not None:
            return result
    return None
