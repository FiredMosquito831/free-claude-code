"""Auth header strategies for native Anthropic Messages upstreams.

Upstreams that speak the Anthropic Messages protocol do not agree on how a
request is authenticated. Command Code authenticates a plan key with a bearer
token; Anthropic's own API authenticates a Console key with ``x-api-key`` and
requires an ``anthropic-version`` header on every request.

The transport therefore owns the wire protocol and delegates the credential to
one of these strategies, so a new upstream contributes headers rather than a
second copy of the streaming loop.
"""

from typing import Protocol, runtime_checkable

# Anthropic requires an explicit API version on every Messages request. The
# value is a dated contract rather than a "latest" alias, so it is pinned here
# and moves only as a deliberate change.
ANTHROPIC_API_VERSION = "2023-06-01"


@runtime_checkable
class AnthropicMessagesAuth(Protocol):
    """Supply the per-request headers that authenticate one upstream call."""

    async def headers(self) -> dict[str, str]:
        """Return auth headers, refreshing a short-lived credential if needed."""
        ...


class BearerTokenAuth:
    """Authenticate with ``Authorization: Bearer`` (the Command Code shape)."""

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        self._token = token

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


class ApiKeyAuth:
    """Authenticate with an Anthropic Console API key (``x-api-key``)."""

    __slots__ = ("_api_key", "_version")

    def __init__(self, api_key: str, *, version: str = ANTHROPIC_API_VERSION) -> None:
        self._api_key = api_key
        self._version = version

    async def headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._version,
        }
