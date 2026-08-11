"""Tests for honouring the upstream's own rate-limit reset headers."""

import httpx
import pytest

from my_claude_code.providers.failure_policy import (
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    MAX_RATE_LIMIT_COOLDOWN_SECONDS,
    rate_limit_cooldown_seconds,
)


def _error(headers: dict[str, str]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://upstream.invalid/v1/chat")
    response = httpx.Response(429, headers=headers, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "5"}, 5.0),
        ({"retry-after": "0"}, 0.0),
        ({"retry-after-ms": "2500"}, 2.5),
        ({"x-ratelimit-reset-requests": "6m0s"}, 360.0),
        ({"x-ratelimit-reset-requests": "250ms"}, 0.25),
        ({"x-ratelimit-reset-requests": "45m30s"}, 2730.0),
        ({"ratelimit-reset": "12"}, 12.0),
    ],
)
def test_reset_headers_are_honoured(headers, expected) -> None:
    assert rate_limit_cooldown_seconds(_error(headers)) == pytest.approx(expected)


@pytest.mark.parametrize(
    "headers",
    [{}, {"retry-after": "soon"}, {"retry-after": ""}, {"unrelated": "1"}],
)
def test_unusable_headers_fall_back_to_the_default(headers) -> None:
    assert (
        rate_limit_cooldown_seconds(_error(headers))
        == DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
    )


def test_absurd_reset_is_capped() -> None:
    """A bad upstream value must not bench a credential for a week."""
    assert (
        rate_limit_cooldown_seconds(_error({"retry-after": "999999"}))
        == MAX_RATE_LIMIT_COOLDOWN_SECONDS
    )


def test_retry_after_ms_wins_over_coarser_headers() -> None:
    """The millisecond form is the most precise, so it is read first."""
    error = _error({"retry-after-ms": "1500", "retry-after": "60"})
    assert rate_limit_cooldown_seconds(error) == pytest.approx(1.5)


def test_an_exception_without_a_response_uses_the_default() -> None:
    assert (
        rate_limit_cooldown_seconds(RuntimeError("no response"))
        == DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
    )


def test_rate_limited_callback_receives_the_upstream_reset() -> None:
    """The classifier must pass the parsed value, not a hardcoded minute."""
    from my_claude_code.providers.failure_policy import classify_provider_failure

    seen: list[float] = []
    failure = classify_provider_failure(
        _error({"retry-after": "7"}),
        provider_name="test",
        read_timeout_s=60.0,
        request_id="req",
        mark_rate_limited=seen.append,
    )
    assert failure.status_code == 429
    assert seen == [7.0]
