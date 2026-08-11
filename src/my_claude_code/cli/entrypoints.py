"""Lightweight entry points for installed commands.

Both the legacy ``free-claude-code`` owner and the native ``my-claude-code``
owner register console scripts that delegate to these same implementations, so
the two command families are interchangeable entry points.
"""

import sys
from collections.abc import Sequence

from my_claude_code.core.identity import owner_for_invocation
from my_claude_code.core.version import package_version


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
