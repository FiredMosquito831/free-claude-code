"""Shared Claude Code environment policy for FCC client surfaces."""

from collections.abc import Mapping

from free_claude_code.config.proxy_auth import proxy_auth_token

CLAUDE_CODE_AUTO_COMPACT_WINDOW = "190000"
CLAUDE_BINARY_NAME = "claude"


def build_claude_proxy_env(
    *,
    proxy_root_url: str,
    auth_token: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return the canonical environment for Claude Code proxy sessions."""

    # Claude's aggregate traffic flag also suppresses gateway model discovery.
    env = {
        key: value
        for key, value in base_env.items()
        if not key.startswith("ANTHROPIC_")
        and key != "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
    }
    env["ANTHROPIC_BASE_URL"] = proxy_root_url
    env["ANTHROPIC_AUTH_TOKEN"] = proxy_auth_token(auth_token)
    env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = CLAUDE_CODE_AUTO_COMPACT_WINDOW
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_FEEDBACK_COMMAND"] = "1"
    env["DISABLE_ERROR_REPORTING"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    return env


def build_minimal_claude_proxy_env(
    *,
    proxy_root_url: str,
    auth_token: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return the inherited environment with only the proxy variables set.

    Claude Code's `~/.claude/settings.json` takes precedence over environment
    variables, so `build_claude_proxy_env`'s aggressive stripping and extra
    flags are wasted effort for users who already configured that file. This
    builder is for users who have not: it sets exactly `ANTHROPIC_BASE_URL`
    and `ANTHROPIC_AUTH_TOKEN` on top of the inherited process environment,
    removing nothing and adding nothing else.
    """

    env = dict(base_env)
    env["ANTHROPIC_BASE_URL"] = proxy_root_url
    env["ANTHROPIC_AUTH_TOKEN"] = proxy_auth_token(auth_token)
    return env
