"""Read and patch Claude Code's settings.json to point at the FCC proxy."""

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from free_claude_code.config.paths import claude_local_settings_path

CLAUDE_BASE_URL_ENV = "ANTHROPIC_BASE_URL"
CLAUDE_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"
CLAUDE_SETTINGS_BACKUP_SUFFIX = ".fcc-backup"


class ClaudeSettingsError(Exception):
    """Raised when the Claude settings file cannot be read or written."""


@dataclass(frozen=True)
class ClaudeSettingsStatus:
    """Snapshot of a Claude settings.json file relative to the expected FCC proxy env."""

    path: str
    exists: bool
    parsed: bool
    error: str | None
    state: str
    current_base_url: str | None
    base_url_matches: bool
    auth_token_present: bool
    auth_token_matches: bool
    expected_base_url: str
    local_override: str | None


def _load_document(path: Path) -> tuple[dict[str, object] | None, bool, str | None]:
    """Load a JSON object document, returning (data, parsed, error)."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, False, str(exc)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, False, str(exc)

    if not isinstance(data, dict):
        return None, False, "top-level JSON value is not an object"

    return data, True, None


def _env_block(data: dict[str, object]) -> dict[str, object]:
    """Return the document's env block, rejecting a present-but-wrong-shaped value.

    A non-object ``env`` is not something we can safely merge into, and silently
    replacing it would destroy whatever the user meant by it. Treat it like a
    parse failure so every caller refuses instead of clobbering.
    """

    env = data.get("env", {})
    if not isinstance(env, dict):
        raise ClaudeSettingsError('"env" is present but is not a JSON object')
    return {str(key): value for key, value in env.items()}


def _detect_local_override(path: Path) -> str | None:
    """Return the sibling settings.local.json path if it also sets ANTHROPIC_* env keys."""

    local_path = claude_local_settings_path(path)
    if not local_path.exists():
        return None

    data, parsed, _error = _load_document(local_path)
    if not parsed or data is None:
        return None

    env = data.get("env")
    if not isinstance(env, dict):
        return None

    if CLAUDE_BASE_URL_ENV in env or CLAUDE_AUTH_TOKEN_ENV in env:
        return str(local_path)

    return None


def read_status(
    *, path: Path, expected_base_url: str, expected_auth_token: str
) -> ClaudeSettingsStatus:
    """Return the current state of a Claude settings.json file relative to the FCC proxy."""

    path = path.absolute()

    if not path.exists():
        return ClaudeSettingsStatus(
            path=str(path),
            exists=False,
            parsed=True,
            error=None,
            state="unset",
            current_base_url=None,
            base_url_matches=False,
            auth_token_present=False,
            auth_token_matches=False,
            expected_base_url=expected_base_url,
            local_override=_detect_local_override(path),
        )

    data, parsed, error = _load_document(path)
    if not parsed or data is None:
        return ClaudeSettingsStatus(
            path=str(path),
            exists=True,
            parsed=False,
            error=error,
            state="unreadable",
            current_base_url=None,
            base_url_matches=False,
            auth_token_present=False,
            auth_token_matches=False,
            expected_base_url=expected_base_url,
            local_override=_detect_local_override(path),
        )

    try:
        env = _env_block(data)
    except ClaudeSettingsError as exc:
        return ClaudeSettingsStatus(
            path=str(path),
            exists=True,
            parsed=False,
            error=str(exc),
            state="unreadable",
            current_base_url=None,
            base_url_matches=False,
            auth_token_present=False,
            auth_token_matches=False,
            expected_base_url=expected_base_url,
            local_override=_detect_local_override(path),
        )

    current_base_url = env.get(CLAUDE_BASE_URL_ENV)
    current_base_url = current_base_url if isinstance(current_base_url, str) else None
    base_url_matches = current_base_url == expected_base_url

    current_auth_token = env.get(CLAUDE_AUTH_TOKEN_ENV)
    auth_token_present = (
        isinstance(current_auth_token, str) and current_auth_token != ""
    )
    auth_token_matches = (
        isinstance(current_auth_token, str)
        and current_auth_token == expected_auth_token
    )

    base_url_key_present = CLAUDE_BASE_URL_ENV in env
    auth_token_key_present = CLAUDE_AUTH_TOKEN_ENV in env

    if base_url_matches and auth_token_matches:
        state = "configured"
    elif base_url_key_present or auth_token_key_present:
        state = "mismatch"
    else:
        state = "unset"

    return ClaudeSettingsStatus(
        path=str(path),
        exists=True,
        parsed=True,
        error=None,
        state=state,
        current_base_url=current_base_url,
        base_url_matches=base_url_matches,
        auth_token_present=auth_token_present,
        auth_token_matches=auth_token_matches,
        expected_base_url=expected_base_url,
        local_override=_detect_local_override(path),
    )


def _backup_if_needed(path: Path) -> None:
    """Copy the existing settings file to its backup path, once."""

    backup_path = path.with_name(path.name + CLAUDE_SETTINGS_BACKUP_SUFFIX)
    if path.exists() and not backup_path.exists():
        shutil.copyfile(path, backup_path)


def _write_document_atomically(path: Path, data: dict[str, object]) -> None:
    """Write a JSON document to path atomically, creating parent directories as needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".fcc-tmp")
    content = json.dumps(data, indent=2) + "\n"
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def apply_proxy_env(
    *, path: Path, base_url: str, auth_token: str
) -> ClaudeSettingsStatus:
    """Set ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN in the Claude settings file."""

    path = path.absolute()

    data: dict[str, object] = {}
    if path.exists():
        loaded, parsed, error = _load_document(path)
        if not parsed or loaded is None:
            raise ClaudeSettingsError(
                f"cannot parse Claude settings file {path}: {error}"
            )
        data = loaded

    env = dict(_env_block(data))

    try:
        _backup_if_needed(path)

        env[CLAUDE_BASE_URL_ENV] = base_url
        env[CLAUDE_AUTH_TOKEN_ENV] = auth_token
        data["env"] = env

        _write_document_atomically(path, data)
    except OSError as exc:
        raise ClaudeSettingsError(
            f"cannot write Claude settings file {path}: {exc}"
        ) from exc

    return read_status(
        path=path, expected_base_url=base_url, expected_auth_token=auth_token
    )


def clear_proxy_env(
    *, path: Path, expected_base_url: str = "", expected_auth_token: str = ""
) -> ClaudeSettingsStatus:
    """Remove ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN from the Claude settings file.

    The expected values do not affect what is removed; they are only carried into
    the returned status so a caller can keep rendering what a re-apply would write.
    """

    path = path.absolute()
    status_args = {
        "expected_base_url": expected_base_url,
        "expected_auth_token": expected_auth_token,
    }

    if not path.exists():
        return read_status(path=path, **status_args)

    data, parsed, error = _load_document(path)
    if not parsed or data is None:
        raise ClaudeSettingsError(f"cannot parse Claude settings file {path}: {error}")

    env = _env_block(data)

    if CLAUDE_BASE_URL_ENV not in env and CLAUDE_AUTH_TOKEN_ENV not in env:
        return read_status(path=path, **status_args)

    try:
        _backup_if_needed(path)

        env = dict(env)
        env.pop(CLAUDE_BASE_URL_ENV, None)
        env.pop(CLAUDE_AUTH_TOKEN_ENV, None)
        if env:
            data["env"] = env
        else:
            data.pop("env", None)

        _write_document_atomically(path, data)
    except OSError as exc:
        raise ClaudeSettingsError(
            f"cannot write Claude settings file {path}: {exc}"
        ) from exc

    return read_status(path=path, **status_args)
