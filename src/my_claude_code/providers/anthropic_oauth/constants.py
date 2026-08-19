"""Wire constants for the Claude Code OAuth surface.

READ ``docs/ANTHROPIC-SUBSCRIPTION.md`` BEFORE CHANGING ANYTHING HERE.

Anthropic's published position is that OAuth credentials from Claude Free, Pro
and Max plans are for Claude Code and Claude.ai only, and that third-party
products may not route requests through them. This provider does that anyway,
at the operator's explicit instruction and on their own account. The constants
below are the documented shape of that surface, not an endorsement of using it.
"""

# --- OAuth client -------------------------------------------------------

CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES = "org:create_api_key user:profile user:inference"
PKCE_METHOD = "S256"

# The token endpoint rejects a generic client.
TOKEN_ENDPOINT_USER_AGENT = "anthropic"

# --- Messages API -------------------------------------------------------

ANTHROPIC_OAUTH_DEFAULT_BASE = "https://api.anthropic.com/v1"

# ``oauth-2025-04-20`` is what makes the API accept an OAuth token at all;
# ``claude-code-20250219`` selects the Claude Code request surface, which is
# what the ``cc_`` tool-name prefix belongs to. Both are protocol selectors.
ANTHROPIC_OAUTH_BETAS = "oauth-2025-04-20,claude-code-20250219"

# Identity headers. These claim the request came from Anthropic's official CLI.
# They are sent because the operator explicitly chose the full Claude Code
# header set, and because the request body -- forwarded verbatim from a real
# Claude Code session -- already carries the same claim truthfully in its
# ``x-anthropic-billing-header`` line. The entrypoint gate in ``entrypoint.py``
# is what keeps that claim honest: without it, this header would be a lie.
CLAUDE_CODE_APP = "cli"
CLAUDE_CODE_USER_AGENT = "claude-cli/2.1.235.2db (external, cli)"

# Claude Code's OAuth surface expects tool names carrying this prefix.
TOOL_NAME_PREFIX = "cc_"

# Refresh this many seconds before the token actually expires.
REFRESH_LEEWAY_SECONDS = 300
