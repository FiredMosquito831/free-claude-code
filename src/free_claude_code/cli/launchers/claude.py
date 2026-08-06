"""Installed `fcc-claude` and `fcc-claude-old` launchers."""

import os
import sys
from collections.abc import Callable, Mapping, Sequence

from free_claude_code.cli.claude_env import (
    CLAUDE_BINARY_NAME,
    build_claude_proxy_env,
    build_minimal_claude_proxy_env,
)
from free_claude_code.config.server_urls import local_proxy_root_url
from free_claude_code.config.settings import get_settings

from .common import preflight_proxy, resolve_client_binary, run_client_process

_DISPLAY_NAME = "Claude Code"
_INSTALL_HINT = "Install Claude Code with: npm install -g @anthropic-ai/claude-code"
_DISCOVER_MODELS_FLAG = "--discover-models"

_ClaudeEnvBuilder = Callable[..., dict[str, str]]


def _split_discover_models_flag(argv: Sequence[str]) -> tuple[bool, list[str]]:
    """Strip `--discover-models` from the leading section of argv.

    Only occurrences before the first bare `--` separator are treated as the
    flag and removed; everything at or after a bare `--` is passed through
    untouched, since Claude Code treats it as literal argument text (e.g. a
    `-p` prompt value) rather than a flag for us to interpret. This means a
    bare `--discover-models` occurring after a `--` separator is kept as-is,
    while any occurrence before it — including a repeated one — is treated
    as the flag and stripped.
    """

    if _DISCOVER_MODELS_FLAG not in argv:
        return False, list(argv)

    try:
        separator_index = argv.index("--")
    except ValueError:
        separator_index = len(argv)

    leading = argv[:separator_index]
    trailing = argv[separator_index:]
    found = _DISCOVER_MODELS_FLAG in leading
    remaining = [arg for arg in leading if arg != _DISCOVER_MODELS_FLAG] + list(
        trailing
    )
    return found, remaining


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Claude Code with only the proxy URL and auth token set.

    Accepts an FCC-only `--discover-models` flag (stripped before Claude
    Code ever sees the argument list) that enables the FCC model catalog
    fetch used by Claude Code's native model picker.
    """

    args = list(sys.argv[1:] if argv is None else argv)
    enable_model_discovery, args = _split_discover_models_flag(args)
    _launch_claude(
        args,
        build_env=build_minimal_claude_proxy_env,
        extra_env_kwargs={"enable_model_discovery": enable_model_discovery},
    )


def launch_legacy(argv: Sequence[str] | None = None) -> None:
    """Launch Claude Code with the full Free Claude Code proxy environment."""

    _launch_claude(argv, build_env=build_claude_proxy_env)


def _launch_claude(
    argv: Sequence[str] | None,
    *,
    build_env: _ClaudeEnvBuilder,
    extra_env_kwargs: Mapping[str, object] | None = None,
) -> None:
    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if error := preflight_proxy(proxy_root_url):
        print(
            f"Free Claude Code proxy is not reachable at {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Start it in another terminal with: fcc-server", file=sys.stderr)
        raise SystemExit(1)

    binary_name = claude_binary_name()
    binary_path = resolve_client_binary(
        binary_name=binary_name,
        display_name=_DISPLAY_NAME,
        install_hint=_INSTALL_HINT,
    )
    args = list(sys.argv[1:] if argv is None else argv)
    run_client_process(
        command=build_claude_launcher_command(binary_path=binary_path, argv=args),
        env=build_env(
            proxy_root_url=proxy_root_url,
            auth_token=settings.anthropic_auth_token,
            base_env=os.environ,
            **(extra_env_kwargs or {}),
        ),
        binary_name=binary_name,
        display_name=_DISPLAY_NAME,
        install_hint=_INSTALL_HINT,
    )


def claude_binary_name() -> str:
    """Return the Claude Code binary name."""

    return CLAUDE_BINARY_NAME


def build_claude_launcher_command(
    *, binary_path: str, argv: Sequence[str]
) -> list[str]:
    """Return the Claude wrapper command without changing user arguments."""

    return [binary_path, *argv]
