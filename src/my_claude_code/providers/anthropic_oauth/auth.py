"""Auth strategy presenting a Claude subscription OAuth token."""

import asyncio

from my_claude_code.providers.anthropic_messages import ANTHROPIC_API_VERSION

from .constants import (
    ANTHROPIC_OAUTH_BETAS,
    CLAUDE_CODE_APP,
    CLAUDE_CODE_USER_AGENT,
)
from .credentials import OAuthTokens, load_tokens, refresh_tokens


class AnthropicOAuthAuth:
    """Present the subscription credential, refreshing it as it ages.

    The token is resolved per request rather than captured once, because a
    long-lived server outlives any single access token. One lock serialises
    refreshes so a burst of concurrent requests performs one exchange rather
    than one each -- a refresh storm against the token endpoint is exactly the
    "unusual traffic pattern" this provider is already close enough to.
    """

    def __init__(self, tokens: OAuthTokens | None = None) -> None:
        self._tokens = tokens
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> OAuthTokens | None:
        return self._tokens

    async def current_tokens(self) -> OAuthTokens:
        async with self._lock:
            if self._tokens is None:
                self._tokens = load_tokens()
            if self._tokens.needs_refresh() and self._tokens.has_refresh_token:
                self._tokens = await refresh_tokens(self._tokens)
            return self._tokens

    async def headers(self) -> dict[str, str]:
        tokens = await self.current_tokens()
        return {
            # The OAuth surface takes the subscription token in x-api-key.
            "x-api-key": tokens.access_token,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "anthropic-beta": ANTHROPIC_OAUTH_BETAS,
            "anthropic-dangerous-direct-browser-access": "true",
            "x-app": CLAUDE_CODE_APP,
            "user-agent": CLAUDE_CODE_USER_AGENT,
        }
