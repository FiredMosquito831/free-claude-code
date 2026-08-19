"""Load, store and refresh Claude subscription OAuth credentials.

Two sources, in precedence order:

1. **MCC's own store** (``~/.fcc/anthropic_oauth.json``), written by
   ``mcc-anthropic-oauth-login``. Preferred, because MCC may refresh it without
   touching state Claude Code owns.
2. **Claude Code's own credential file** (``~/.claude/.credentials.json``,
   ``claudeAiOauth`` object), read only when MCC has no store of its own.

Reading source 2 is deliberately read-only and never refreshed in place: that
file belongs to Claude Code, a refresh rotates the token, and racing its owner
would log the user out of their real client. When a token read from there is
close to expiry, MCC refreshes into *its own* store and leaves the original
alone.

See ``docs/ANTHROPIC-SUBSCRIPTION.md`` for the policy position on using these
credentials at all.
"""

import contextlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.config.paths import (
    anthropic_oauth_managed_store_path,
)

from .constants import (
    CLAUDE_CODE_CLIENT_ID,
    REFRESH_LEEWAY_SECONDS,
    TOKEN_ENDPOINT_USER_AGENT,
    TOKEN_URL,
)

CLAUDE_CREDENTIALS_DIRNAME = ".claude"
CLAUDE_CREDENTIALS_FILENAME = ".credentials.json"
CLAUDE_OAUTH_KEY = "claudeAiOauth"


class AnthropicOAuthRefreshError(RuntimeError):
    """Raised when Anthropic rejects or cannot complete a token refresh."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Anthropic OAuth refresh failed with HTTP {status_code}")


class AnthropicOAuthUnavailableError(RuntimeError):
    """Raised when no subscription credential can be found at all."""


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """One Claude subscription OAuth credential set."""

    access_token: str
    refresh_token: str | None = None
    expires_at: int | None = None
    scopes: tuple[str, ...] = ()
    subscription_type: str | None = None
    # Where this came from, for diagnostics. Never contains a secret.
    source: str = "unknown"

    @property
    def has_access_token(self) -> bool:
        return bool(self.access_token.strip())

    @property
    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token and self.refresh_token.strip())

    def seconds_remaining(self, *, now: float | None = None) -> float | None:
        """Seconds until expiry, or ``None`` when the token reports none."""
        if self.expires_at is None:
            return None
        return self.expires_at - (time.time() if now is None else now)

    def needs_refresh(self, *, now: float | None = None) -> bool:
        remaining = self.seconds_remaining(now=now)
        if remaining is None:
            return False
        return remaining <= REFRESH_LEEWAY_SECONDS


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _home() -> Path:
    return Path.home()


def managed_store_path() -> Path:
    """Where MCC keeps the credential it owns and may refresh."""
    return anthropic_oauth_managed_store_path()


def claude_credentials_path() -> Path:
    """Claude Code's own credential file.

    Honours ``CLAUDE_CONFIG_DIR``, which Claude Code documents as relocating
    ``.credentials.json`` on Linux and Windows.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override) / CLAUDE_CREDENTIALS_FILENAME
    return _home() / CLAUDE_CREDENTIALS_DIRNAME / CLAUDE_CREDENTIALS_FILENAME


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError, ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _expiry_seconds(payload: dict[str, Any]) -> int | None:
    """Normalise Anthropic's millisecond ``expiresAt`` to epoch seconds."""
    for key in ("expiresAt", "expires_at"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # Claude Code stores milliseconds; the token endpoint returns
            # seconds. Anything past year ~2286 in seconds is really millis.
            return int(value / 1000) if value > 10_000_000_000 else int(value)
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        return int(time.time() + expires_in)
    return None


def _scopes(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("scopes") or payload.get("scope")
    if isinstance(raw, str):
        return tuple(part for part in raw.split() if part)
    if isinstance(raw, list):
        return tuple(str(part) for part in raw if str(part).strip())
    return ()


def _tokens_from_payload(payload: dict[str, Any], *, source: str) -> OAuthTokens | None:
    access = payload.get("accessToken") or payload.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    refresh = payload.get("refreshToken") or payload.get("refresh_token")
    subscription = payload.get("subscriptionType") or payload.get("subscription_type")
    return OAuthTokens(
        access_token=access.strip(),
        refresh_token=refresh.strip() if isinstance(refresh, str) else None,
        expires_at=_expiry_seconds(payload),
        scopes=_scopes(payload),
        subscription_type=subscription if isinstance(subscription, str) else None,
        source=source,
    )


def load_managed_tokens() -> OAuthTokens | None:
    """Read the credential MCC owns, if one has been stored."""
    return _tokens_from_payload(_load_json(managed_store_path()), source="mcc")


def load_claude_code_tokens() -> OAuthTokens | None:
    """Read Claude Code's own credential file, without modifying it."""
    payload = _load_json(claude_credentials_path())
    oauth = payload.get(CLAUDE_OAUTH_KEY)
    if not isinstance(oauth, dict):
        return None
    return _tokens_from_payload(oauth, source="claude-code")


def detect_available_sources() -> dict[str, bool]:
    """Report which credential sources exist, without reading any secret.

    The admin UI uses this to offer "use the credentials already on this
    machine" versus "sign in", so it must never surface a token value.
    """
    return {
        "mcc": load_managed_tokens() is not None,
        "claude_code": load_claude_code_tokens() is not None,
    }


def load_tokens() -> OAuthTokens:
    """Return the credential to use, preferring MCC's own store."""
    for loader in (load_managed_tokens, load_claude_code_tokens):
        tokens = loader()
        if tokens is not None and tokens.has_access_token:
            return tokens
    raise AnthropicOAuthUnavailableError(
        "No Claude subscription credential found. Either sign in with "
        "`mcc-anthropic-oauth-login`, or log in to Claude Code so that "
        f"{claude_credentials_path()} exists."
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON 0600, atomically, so a token is never world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    # Windows inherits the profile directory's ACL; chmod is a no-op there.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def store_tokens(tokens: OAuthTokens) -> None:
    """Persist a credential into MCC's own store."""
    _atomic_write_private_json(
        managed_store_path(),
        {
            "accessToken": tokens.access_token,
            "refreshToken": tokens.refresh_token,
            "expiresAt": tokens.expires_at,
            "scopes": list(tokens.scopes),
            "subscriptionType": tokens.subscription_type,
        },
    )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _refresh_payload(refresh_token: str) -> dict[str, str]:
    return {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLAUDE_CODE_CLIENT_ID,
    }


def _tokens_from_refresh(
    payload: dict[str, Any],
    *,
    previous: OAuthTokens,
) -> OAuthTokens:
    refreshed = _tokens_from_payload(payload, source="mcc")
    if refreshed is None:
        raise AnthropicOAuthRefreshError(200)
    # Anthropic may omit the refresh token on a successful refresh; keeping the
    # previous one is what stops the credential becoming unrenewable.
    if not refreshed.has_refresh_token:
        refreshed = replace(refreshed, refresh_token=previous.refresh_token)
    return refreshed


async def refresh_tokens(tokens: OAuthTokens) -> OAuthTokens:
    """Exchange a refresh token for a fresh credential and store it.

    The result is always written to MCC's own store, never back into Claude
    Code's file: rotating the token there would invalidate the copy the user's
    real client is holding.
    """
    if not tokens.has_refresh_token:
        raise AnthropicOAuthRefreshError(400)
    assert tokens.refresh_token is not None

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL,
            json=_refresh_payload(tokens.refresh_token),
            headers={
                "Content-Type": "application/json",
                "User-Agent": TOKEN_ENDPOINT_USER_AGENT,
            },
        )
    if response.status_code >= 400:
        raise AnthropicOAuthRefreshError(response.status_code)

    refreshed = _tokens_from_refresh(response.json(), previous=tokens)
    store_tokens(refreshed)
    logger.info(
        "Refreshed Claude subscription OAuth credential (expires_at={})",
        refreshed.expires_at,
    )
    return refreshed
