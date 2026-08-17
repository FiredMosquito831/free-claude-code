"""Detect forced Anthropic web server tool requests and read their parameters."""

from dataclasses import dataclass

from my_claude_code.core.anthropic import MessagesRequest, Tool

from .parsers import content_text


@dataclass(frozen=True, slots=True)
class WebSearchToolOptions:
    """Parameters the client declared on its ``web_search`` tool definition.

    Anthropic documents ``max_uses``, ``allowed_domains``, ``blocked_domains``
    and ``user_location`` on the tool itself rather than on the tool call, so
    they arrive as extra fields on :class:`Tool` (which allows extras).
    """

    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    max_uses: int | None = None


def _domain_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(str(entry).strip() for entry in raw if str(entry).strip())


def web_search_tool_options(request: MessagesRequest) -> WebSearchToolOptions:
    """Read search parameters off the request's ``web_search`` tool definition.

    Anthropic rejects a request carrying both allow and block lists, so a
    caller sending both is honoured on the allow list alone rather than
    silently intersecting them.
    """

    for tool in request.tools or []:
        if tool.name != "web_search":
            continue
        extra = tool.model_extra or {}
        allowed = _domain_tuple(extra.get("allowed_domains"))
        blocked = () if allowed else _domain_tuple(extra.get("blocked_domains"))
        raw_max_uses = extra.get("max_uses")
        max_uses = raw_max_uses if isinstance(raw_max_uses, int) else None
        return WebSearchToolOptions(
            allowed_domains=allowed,
            blocked_domains=blocked,
            max_uses=max_uses,
        )
    return WebSearchToolOptions()


def forced_tool_turn_text(request: MessagesRequest) -> str:
    """Text for parsing forced server-tool inputs: latest user turn only (avoids stale history)."""
    if not request.messages:
        return ""

    for message in reversed(request.messages):
        if message.role == "user":
            return content_text(message.content)
    return ""


def selected_server_tool_name(request: MessagesRequest) -> str | None:
    """Return the one local server tool selected without ambiguity.

    Claude Code's WebSearch and WebFetch helpers send a dedicated request with
    one server-tool definition and ``tool_choice=auto``. Explicit tool choices
    remain supported. An auto request with multiple tools belongs to the
    upstream model and must not be intercepted here.
    """

    tool_choice = request.tool_choice
    if not isinstance(tool_choice, dict):
        return None
    if tool_choice.get("type") == "tool":
        name = tool_choice.get("name")
        return str(name) if name in {"web_search", "web_fetch"} else None
    tools = request.tools or []
    if tool_choice.get("type") != "auto" or len(tools) != 1:
        return None
    name = tools[0].name
    return name if name in {"web_search", "web_fetch"} else None


def has_tool_named(request: MessagesRequest, name: str) -> bool:
    return any(tool.name == name for tool in request.tools or [])


def is_web_server_tool_request(request: MessagesRequest) -> bool:
    """True when one local server tool is selected without ambiguity."""

    selected = selected_server_tool_name(request)
    if selected is None:
        return False
    return has_tool_named(request, selected)


def is_anthropic_server_tool_definition(tool: Tool) -> bool:
    """Whether ``tool`` refers to an Anthropic server tool (web_search / web_fetch family)."""
    name = (tool.name or "").strip()
    if name in ("web_search", "web_fetch"):
        return True
    typ = tool.type
    if isinstance(typ, str):
        return typ.startswith("web_search") or typ.startswith("web_fetch")
    return False


def has_listed_anthropic_server_tools(request: MessagesRequest) -> bool:
    """True when tools include web_search / web_fetch-style entries (listed, forced or not)."""
    return any(is_anthropic_server_tool_definition(t) for t in (request.tools or []))


def unsupported_server_tool_error(
    request: MessagesRequest, *, web_tools_enabled: bool
) -> str | None:
    """Return the user-facing error when the resolved provider cannot run server tools."""
    selected = selected_server_tool_name(request)
    if selected and not web_tools_enabled:
        return (
            f"Anthropic server tool {selected!r} is selected, but local web server tools are "
            "disabled (ENABLE_WEB_SERVER_TOOLS=false). Enable them or remove the server tool."
        )
    if not selected and has_listed_anthropic_server_tools(request):
        return (
            "MCC cannot pass ambiguous Anthropic server tools (web_search / web_fetch) "
            "to OpenAI Chat upstreams. Use a dedicated single-tool request with "
            "tool_choice=auto, force one tool, or remove these tools."
        )
    return None
