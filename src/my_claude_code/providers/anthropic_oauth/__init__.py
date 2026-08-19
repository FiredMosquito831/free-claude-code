"""Claude subscription OAuth provider.

Using a Claude Free/Pro/Max OAuth credential from a third-party product is
against Anthropic's published terms. See ``docs/ANTHROPIC-SUBSCRIPTION.md``.
"""

from .auth import AnthropicOAuthAuth
from .cli import anthropic_oauth_login_command
from .credentials import (
    AnthropicOAuthRefreshError,
    AnthropicOAuthUnavailableError,
    OAuthTokens,
    claude_credentials_path,
    detect_available_sources,
    load_claude_code_tokens,
    load_managed_tokens,
    load_tokens,
    managed_store_path,
    refresh_tokens,
    store_tokens,
)
from .entrypoint import (
    CLI_ENTRYPOINT,
    detect_client_version,
    detect_entrypoint,
    is_claude_code_cli,
)
from .provider import PROVIDER_NAME, AnthropicOAuthProvider
from .tool_names import add_prefix, apply_tool_prefix, strip_prefix

__all__ = [
    "CLI_ENTRYPOINT",
    "PROVIDER_NAME",
    "AnthropicOAuthAuth",
    "AnthropicOAuthProvider",
    "AnthropicOAuthRefreshError",
    "AnthropicOAuthUnavailableError",
    "OAuthTokens",
    "add_prefix",
    "anthropic_oauth_login_command",
    "apply_tool_prefix",
    "claude_credentials_path",
    "detect_available_sources",
    "detect_client_version",
    "detect_entrypoint",
    "is_claude_code_cli",
    "load_claude_code_tokens",
    "load_managed_tokens",
    "load_tokens",
    "managed_store_path",
    "refresh_tokens",
    "store_tokens",
    "strip_prefix",
]
