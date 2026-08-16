"""Lightweight entry points for installed commands.

Both the legacy ``free-claude-code`` owner and the native ``my-claude-code``
owner register console scripts that delegate to these same implementations, so
the two command families are interchangeable entry points.
"""

import sys
from collections.abc import Sequence

from my_claude_code.core.identity import owner_for_invocation
from my_claude_code.core.version import package_version


def help_command(argv: Sequence[str] | None = None) -> None:
    """Print a short reference for every command and how to use it."""
    del argv  # --version is handled by the entry point; otherwise no options.
    print(_help_text())


def _help_text() -> str:
    return """My Claude Code -- commands

The proxy runs on your machine and routes your coding agents to the models and
providers you configure. Everything is local: your keys stay in ~/.fcc.

Start the proxy:
  mcc-server              Start the local proxy and admin dashboard
  my-claude-code          Same as mcc-server (full command name)

Use a coding agent through the proxy:
  mcc-claude              Launch Claude Code through the proxy
  mcc-claude --discover-models   Also enable the model picker from the catalog
  mcc-claude-old          Legacy launcher: full proxy environment, auto-compact
  mcc-codex               Launch Codex through the proxy
  mcc-pi                  Launch Pi through the proxy
  mcc-desktop             Open the system tray app (desktop)

Manage and inspect:
  mcc-init                Create or repair ~/.fcc/.env with the config template
  mcc-chatgpt-oauth-login Log in to ChatGPT/Codex via OAuth device flow
  mcc-compact-log         Compact the request log (deduplicate + compress)
  mcc-rtk                 Manage the RTK token optimizer
  mcc-help                Show this command reference

The legacy fcc-* commands (fcc-server, fcc-claude, fcc-codex, fcc-pi,
fcc-init, fcc-chatgpt-oauth-login, fcc-compact-log, fcc-rtk, free-claude-code)
are kept as aliases and behave identically.

Updates: install while the server is running. On Windows the update is staged
and completes after you stop and restart the app; on Linux/WSL it applies
immediately and is picked up on the next restart. Run mcc-server again after an
update to start the new version.
"""


def serve(argv: Sequence[str] | None = None) -> None:
    """Start the FastAPI server."""
    if _print_version_if_requested(argv):
        return

    # Keep the server composition root off metadata-only command paths.
    from my_claude_code.cli.commands import serve as run_server

    run_server()


def init(argv: Sequence[str] | None = None) -> None:
    """Scaffold config at ~/.fcc/.env."""
    if _print_version_if_requested(argv):
        return

    # Config initialization shares command infrastructure with the server.
    from my_claude_code.cli.commands import init as initialize_config

    initialize_config()


def chatgpt_oauth_login(argv: Sequence[str] | None = None) -> None:
    """Log in to ChatGPT/Codex via OAuth device flow."""
    if _print_version_if_requested(argv):
        return

    from my_claude_code.cli.commands import chatgpt_oauth_login as run_login

    run_login()


def _print_version_if_requested(argv: Sequence[str] | None) -> bool:
    args = sys.argv[1:] if argv is None else argv
    if "--version" not in args:
        return False
    owner = owner_for_invocation()
    print(f"{owner.distribution} {package_version()}")
    return True


def compact_log(argv: Sequence[str] | None = None) -> None:
    """Compact the request log in place."""
    if _print_version_if_requested(argv):
        return

    from my_claude_code.cli.commands import compact_log as run_compaction

    run_compaction()


def rtk(argv: Sequence[str] | None = None) -> None:
    """Manage the RTK token optimizer."""
    from my_claude_code.cli.rtk_commands import rtk_command

    rtk_command(argv)
