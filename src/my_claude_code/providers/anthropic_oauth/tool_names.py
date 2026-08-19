"""Apply Claude Code's ``cc_`` tool-name prefix on the wire.

The Claude Code OAuth surface expects tool names to carry a ``cc_`` prefix.
The prefix has to be applied consistently across every place a tool name
appears in a request -- definitions, an explicit tool choice, and the
``tool_use``/``tool_result`` blocks in conversation history -- because a name
that is prefixed in one place and bare in another does not resolve.

Responses are normalised back to the unprefixed names so nothing downstream
(the request log, the analytics tool-call view, the client itself) has to know
this happened.
"""

from copy import deepcopy
from typing import Any

from .constants import TOOL_NAME_PREFIX


def add_prefix(name: str) -> str:
    """Prefix one tool name, leaving an already-prefixed name alone."""
    return name if name.startswith(TOOL_NAME_PREFIX) else f"{TOOL_NAME_PREFIX}{name}"


def strip_prefix(name: str) -> str:
    """Remove the wire prefix from one tool name."""
    return name[len(TOOL_NAME_PREFIX) :] if name.startswith(TOOL_NAME_PREFIX) else name


def _rename_blocks(content: Any, rename: Any) -> Any:
    if not isinstance(content, list):
        return content
    for block in content:
        if not isinstance(block, dict):
            continue
        # ``tool_result`` addresses a prior ``tool_use`` by id rather than by
        # name, so only a name field (when a provider includes one) is renamed.
        if block.get("type") in {"tool_use", "tool_result"} and isinstance(
            block.get("name"), str
        ):
            block["name"] = rename(block["name"])
    return content


def apply_tool_prefix(body: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of one Messages body with tool names prefixed."""
    prefixed = deepcopy(body)

    tools = prefixed.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                tool["name"] = add_prefix(tool["name"])

    choice = prefixed.get("tool_choice")
    if isinstance(choice, dict) and isinstance(choice.get("name"), str):
        choice["name"] = add_prefix(choice["name"])

    messages = prefixed.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                _rename_blocks(message.get("content"), add_prefix)

    return prefixed


def strip_tool_prefix_from_frame(frame: str) -> str:
    """Remove the ``cc_`` prefix from a tool name inside one SSE frame.

    Operates on the serialized frame so the streaming path stays a passthrough:
    parsing and re-serializing every frame would cost more than a targeted
    replacement of the one field that can carry a prefixed name.
    """
    marker = f'"name":"{TOOL_NAME_PREFIX}'
    if marker in frame:
        return frame.replace(marker, '"name":"')
    spaced = f'"name": "{TOOL_NAME_PREFIX}'
    if spaced in frame:
        return frame.replace(spaced, '"name": "')
    return frame
