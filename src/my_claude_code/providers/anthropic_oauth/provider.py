"""Claude subscription OAuth provider, gated to the Claude Code CLI.

READ ``docs/ANTHROPIC-SUBSCRIPTION.md`` BEFORE USING OR CHANGING THIS.

Anthropic's published position (code.claude.com/docs/en/legal-and-compliance)
is that OAuth credentials from Claude Free, Pro and Max plans are for Claude
Code and Claude.ai only, and that third-party developers may not route requests
through them. This provider does exactly that. It exists because the operator
asked for it, for their own account, having been shown the policy.

The one thing this provider does to keep its own story straight: it refuses to
touch the subscription credential unless the request genuinely came from the
Claude Code CLI, proven by the ``cc_entrypoint=cli`` marker Claude Code stamps
into the request body. Anything else -- the Agent SDK, another harness, a bare
API call -- is refused here rather than quietly billed to the subscription.
"""

from collections.abc import AsyncIterator

from loguru import logger

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.providers.anthropic import AnthropicProvider
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .auth import AnthropicOAuthAuth
from .credentials import OAuthTokens
from .entrypoint import CLI_ENTRYPOINT, detect_entrypoint, is_claude_code_cli
from .tool_names import apply_tool_prefix, strip_tool_prefix_from_frame

PROVIDER_NAME = "ANTHROPIC_OAUTH"

_REFUSAL = (
    "The Anthropic subscription provider only serves requests that come from "
    "the Claude Code CLI. This request reports cc_entrypoint={reported}, so it "
    "was refused rather than billed to your Claude subscription. Route it to "
    "the `anthropic` provider (Claude Console API key) instead. See "
    "docs/ANTHROPIC-SUBSCRIPTION.md."
)


def _auth_for(config: ProviderConfig) -> AnthropicOAuthAuth:
    """Prefer an explicitly configured token, else discover one from disk.

    A raw ``ANTHROPIC_OAUTH_ACCESS_TOKEN`` carries no refresh token, so it
    expires and stays expired -- the same trap Part IX records for
    ``CHATGPT_OAUTH_ACCESS_TOKEN``. Discovery is the maintained path; the raw
    value exists as an escape hatch and is logged as such.
    """
    raw = (config.api_key or "").strip()
    if not raw:
        return AnthropicOAuthAuth()
    logger.warning(
        "Using a raw ANTHROPIC_OAUTH_ACCESS_TOKEN; it cannot be refreshed and "
        "will stop working when it expires. Prefer mcc-anthropic-oauth-login."
    )
    return AnthropicOAuthAuth(OAuthTokens(access_token=raw, source="env"))


class AnthropicOAuthProvider(AnthropicProvider):
    """Stream Anthropic Messages using a Claude subscription OAuth token."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        rate_limiter: ProviderRateLimiter,
        auth: AnthropicOAuthAuth | None = None,
        require_claude_code_cli: bool = True,
    ) -> None:
        super().__init__(
            config,
            rate_limiter=rate_limiter,
            auth=auth if auth is not None else _auth_for(config),
            provider_name=PROVIDER_NAME,
            body_transform=apply_tool_prefix,
        )
        self._require_claude_code_cli = require_claude_code_cli

    # -- entrypoint gate ---------------------------------------------------

    def _enforce_entrypoint(self, request: MessagesRequest) -> None:
        if not self._require_claude_code_cli:
            return
        if is_claude_code_cli(request):
            return
        reported = detect_entrypoint(request) or "none"
        logger.warning(
            "Refused a non-CLI request on the Claude subscription credential "
            "(cc_entrypoint={}, expected={})",
            reported,
            CLI_ENTRYPOINT,
        )
        raise InvalidRequestError(_REFUSAL.format(reported=reported))

    # -- streaming ---------------------------------------------------------

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        # The gate runs in preflight so a refusal happens before any credential
        # is read and before the fallback chain commits to this hop.
        self._enforce_entrypoint(request)
        super().preflight_stream(request, reasoning=reasoning)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        self._enforce_entrypoint(request)
        return self._stream_with_tool_prefix(
            request,
            input_tokens=input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )

    async def _stream_with_tool_prefix(
        self,
        request: MessagesRequest,
        *,
        input_tokens: int,
        request_id: str | None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        upstream = super().stream_response(
            request,
            input_tokens=input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )
        async for frame in upstream:
            # Normalise the wire prefix away so the request log, the analytics
            # tool-call view and the client never see a cc_-prefixed name.
            yield strip_tool_prefix_from_frame(frame)

    # -- model listing -----------------------------------------------------

    async def list_model_ids(self) -> frozenset[str]:
        """List models with the subscription credential.

        Listing carries no conversation, so the entrypoint gate cannot apply
        and deliberately does not: discovery is what populates the model picker
        and is not a billed inference request.
        """
        return await super().list_model_ids()
