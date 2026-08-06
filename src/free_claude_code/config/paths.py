"""Shared filesystem paths for Free Claude Code configuration."""

import os
from pathlib import Path

FCC_CONFIG_DIRNAME = ".fcc"
FCC_ENV_FILENAME = ".env"
LEGACY_REPO_DIRNAME = "free-claude-code"
LEGACY_XDG_CONFIG_DIRNAME = ".config"
MESSAGING_STATE_DIRNAME = "agent_workspace"
FCC_LOGS_DIRNAME = "logs"
SERVER_LOG_FILENAME = "server.log"
CODEX_MODEL_CATALOG_FILENAME = "codex-model-catalog.json"
AUTH_DIRNAME = "auth"
CHATGPT_OAUTH_AUTH_FILENAME = "chatgpt-oauth.json"
CLAUDE_CONFIG_DIRNAME = ".claude"
CLAUDE_SETTINGS_FILENAME = "settings.json"
CLAUDE_LOCAL_SETTINGS_FILENAME = "settings.local.json"
WSL_OSRELEASE_PATH = "/proc/sys/kernel/osrelease"
WSL_WINDOWS_USERS_DIR = "/mnt/c/Users"


def config_dir_path() -> Path:
    """Return the default user config directory."""

    return Path.home() / FCC_CONFIG_DIRNAME


def managed_env_path() -> Path:
    """Return the default user-managed env file path."""

    return config_dir_path() / FCC_ENV_FILENAME


def legacy_env_paths() -> tuple[Path, ...]:
    """Return legacy user env paths that can be migrated to ~/.fcc/.env."""

    home = Path.home()
    return (
        home / LEGACY_REPO_DIRNAME / FCC_ENV_FILENAME,
        home / LEGACY_XDG_CONFIG_DIRNAME / LEGACY_REPO_DIRNAME / FCC_ENV_FILENAME,
    )


def messaging_state_dir_path() -> Path:
    """Return the managed messaging state directory."""

    return config_dir_path() / MESSAGING_STATE_DIRNAME


def server_log_path() -> Path:
    """Return the canonical server log path."""

    return config_dir_path() / FCC_LOGS_DIRNAME / SERVER_LOG_FILENAME


def codex_model_catalog_path() -> Path:
    """Return the generated Codex model catalog path."""

    return config_dir_path() / CODEX_MODEL_CATALOG_FILENAME


def chatgpt_oauth_auth_path() -> Path:
    """Return FCC's private renewable ChatGPT OAuth credential path."""

    return config_dir_path() / AUTH_DIRNAME / CHATGPT_OAUTH_AUTH_FILENAME


def claude_settings_path() -> Path:
    """Return the default Claude Code settings.json path."""

    return Path.home() / CLAUDE_CONFIG_DIRNAME / CLAUDE_SETTINGS_FILENAME


def claude_local_settings_path(settings_path: Path) -> Path:
    """Return the sibling settings.local.json path for a given settings.json path."""

    return settings_path.parent / CLAUDE_LOCAL_SETTINGS_FILENAME


def _is_wsl() -> bool:
    """Return True when running inside WSL, detected via the kernel osrelease string."""

    try:
        osrelease = Path(WSL_OSRELEASE_PATH).read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in osrelease.lower()


def windows_claude_settings_path() -> Path | None:
    """Return the Windows-side Claude settings.json path when running under WSL.

    Returns None when not running under WSL, or when no plausible Windows user
    directory containing a .claude directory can be found.
    """

    if not _is_wsl():
        return None

    windows_users_dir = Path(WSL_WINDOWS_USERS_DIR)
    try:
        entries = list(windows_users_dir.iterdir())
    except OSError:
        return None

    for entry in entries:
        try:
            if (entry / CLAUDE_CONFIG_DIRNAME).is_dir():
                return entry / CLAUDE_CONFIG_DIRNAME / CLAUDE_SETTINGS_FILENAME
        except OSError:
            continue

    username = os.environ.get("USER") or os.environ.get("USERNAME")
    if username:
        candidate = windows_users_dir / username
        try:
            if candidate.is_dir():
                return candidate / CLAUDE_CONFIG_DIRNAME / CLAUDE_SETTINGS_FILENAME
        except OSError:
            pass

    return None
