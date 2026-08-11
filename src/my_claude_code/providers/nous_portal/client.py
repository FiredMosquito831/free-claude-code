"""Nous Portal provider implementation.

The Nous Research inference API is OpenRouter-dialect: its ``/models`` payload
carries ``supported_parameters``/``canonical_slug`` and reasoning is negotiated
with a ``reasoning`` object, so it reuses the shared gateway behaviour.
"""

from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openrouter_gateway import (
    OpenRouterGatewayProvider,
    openrouter_gateway_profile,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

_PROFILE = openrouter_gateway_profile("NOUS_PORTAL")


class NousPortalProvider(OpenRouterGatewayProvider):
    """Nous Portal provider using the OpenAI-compatible Chat Completions API."""

    def __init__(self, config: ProviderConfig, *, rate_limiter: ProviderRateLimiter):
        super().__init__(config, profile=_PROFILE, rate_limiter=rate_limiter)
