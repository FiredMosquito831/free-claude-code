"""OpenRouter provider implementation."""

from free_claude_code.providers.base import ProviderConfig
from free_claude_code.providers.openrouter_gateway import (
    OpenRouterGatewayProvider,
    openrouter_gateway_profile,
)
from free_claude_code.providers.rate_limit import ProviderRateLimiter

_PROFILE = openrouter_gateway_profile("OPENROUTER")


class OpenRouterProvider(OpenRouterGatewayProvider):
    """OpenRouter provider using the OpenAI-compatible Chat Completions API."""

    def __init__(self, config: ProviderConfig, *, rate_limiter: ProviderRateLimiter):
        super().__init__(config, profile=_PROFILE, rate_limiter=rate_limiter)
