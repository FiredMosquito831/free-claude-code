"""Web search provider error hierarchy with stable error kinds.

The ``kind`` class attribute is the canonical classification consumed by key
rotation (auth -> lockout, rate_limit -> cooldown, etc.) and by analytics.
"""

from typing import ClassVar


class WebSearchError(Exception):
    """Base error for all web search provider failures."""

    kind: ClassVar[str] = "upstream"

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.status_code = status_code
        # Filled in by the rotation loop when a specific key produced the error.
        self.key_index: int | None = None

    def __str__(self) -> str:
        return f"{self.provider}: {self.message}"


class WebSearchConfigError(WebSearchError):
    """Provider is selected but not configured (missing key, missing base URL)."""

    kind: ClassVar[str] = "config"


class WebSearchAuthError(WebSearchError):
    """Authentication/authorization failure (HTTP 401/403, stale keys)."""

    kind: ClassVar[str] = "auth"


class WebSearchRateLimitError(WebSearchError):
    """Rate limit (HTTP 429 or provider-specific throttling signal)."""

    kind: ClassVar[str] = "rate_limit"


class WebSearchQuotaError(WebSearchError):
    """Billing/plan quota exhausted (HTTP 402 or provider-specific status)."""

    kind: ClassVar[str] = "quota"


class WebSearchInvalidRequestError(WebSearchError):
    """The request itself was rejected (HTTP 400/422); rotating keys won't help."""

    kind: ClassVar[str] = "invalid"


class WebSearchUpstreamError(WebSearchError):
    """Upstream 5xx, network/transport failure, or malformed provider payload."""

    kind: ClassVar[str] = "upstream"
