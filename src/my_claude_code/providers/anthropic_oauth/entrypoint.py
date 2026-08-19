"""Detect which Claude Code entrypoint produced a request.

Claude Code stamps an attribution line at the head of the system prompt, in the
request body rather than in an HTTP header:

    x-anthropic-billing-header: cc_version=2.1.235.2db; cc_entrypoint=cli;

Measured on the operator's own traffic: the terminal CLI reports
``cc_entrypoint=cli``, while the Python Agent SDK reports ``cc_entrypoint=sdk-py``.
The marker travels with the body, so it is the one client signal a proxy can
neither forge for traffic it did not receive nor strip from traffic it did.

That property is what this module exists for. The subscription OAuth credential
is only ever used for requests that genuinely came from the Claude Code CLI, so
"only within Claude Code" is enforced by the router rather than promised in a
README. Anything else -- the Agent SDK, another harness, a bare API call -- is
refused here and routed to a provider with its own credential.
"""

import re
from typing import Any

from my_claude_code.core.anthropic.models import MessagesRequest

BILLING_HEADER_MARKER = "x-anthropic-billing-header:"

_ENTRYPOINT_RE = re.compile(r"cc_entrypoint\s*=\s*([A-Za-z0-9._-]+)")
_VERSION_RE = re.compile(r"cc_version\s*=\s*([A-Za-z0-9._-]+)")

# The terminal CLI. Deliberately a single value rather than a prefix match:
# ``sdk-py`` and ``sdk-ts`` are the Agent SDK, which Anthropic's policy names
# explicitly, and a loose match would quietly readmit them.
CLI_ENTRYPOINT = "cli"


def system_prompt_text(request: MessagesRequest) -> str:
    """Return the request's system prompt as flat text.

    ``system`` is either a string or a list of content blocks, and the marker
    sits in the first block, so both shapes have to be flattened before the
    line can be read.
    """
    system: Any = request.system
    if system is None:
        return ""
    if isinstance(system, str):
        return system

    parts: list[str] = []
    for block in system:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def detect_entrypoint(request: MessagesRequest) -> str | None:
    """Return the reported ``cc_entrypoint``, or ``None`` when unmarked."""
    match = _ENTRYPOINT_RE.search(system_prompt_text(request))
    return match.group(1) if match else None


def detect_client_version(request: MessagesRequest) -> str | None:
    """Return the reported ``cc_version``, or ``None`` when unmarked."""
    match = _VERSION_RE.search(system_prompt_text(request))
    return match.group(1) if match else None


def is_claude_code_cli(request: MessagesRequest) -> bool:
    """Return whether this request came from the Claude Code terminal CLI."""
    return detect_entrypoint(request) == CLI_ENTRYPOINT
