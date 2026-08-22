"""Elide the middle of oversized ``Read`` / ``Grep`` / ``Glob`` tool results.

Claude Code resends the whole conversation every turn, tool results included, so
one large file read is paid for again on every subsequent turn. The popular
third-party compressors work as PreToolUse hooks and therefore only ever see
``Bash``; ``Read``, ``Grep`` and ``Glob`` never touch a shell, so nothing on the
client side can reach them. A proxy on the wire can.

That is also why this module is dangerous, and why it is built the way it is:

* **A trim announces itself.** Silently shortening a ``Read`` leaves the model
  believing it saw a whole file. Every elision carries :data:`TRIM_MARKER_OPEN`
  inline at the cut, naming the proxy as the actor and saying how much is gone.
  A trim nobody can see is a bug generator, not an optimization.
* **Ambiguity means hands off.** A ``tool_result`` is only touched when its
  ``tool_use_id`` resolves to exactly one of :data:`TRIMMABLE_TOOL_NAMES`. An
  unmatched id, a duplicated id, an error result, or any content shape this
  module does not fully understand is left byte-for-byte alone.
* **The transform is deterministic.** The same result trims to the same bytes on
  every turn, so a conversation prefix stays stable and Anthropic prompt caching
  keeps hitting after the one turn on which a result first crosses the
  threshold.

Policy -- which rules run, and at what size -- is decided in ``config`` and
handed in as :class:`ToolResultTrimPolicy`. This module owns only the protocol
manipulation, so ``core`` still imports nothing from ``config``.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .content import get_block_attr, get_block_type
from .models import MessagesRequest

# The only tools whose results this module will ever touch. ``Bash`` is
# deliberately absent: client-side compressors already own it, and two layers
# compressing the same bytes makes neither one's savings attributable.
TRIMMABLE_TOOL_NAMES: tuple[str, ...] = ("Read", "Grep", "Glob")

# Deepest nesting walked inside a tool result before giving up. Tool results
# nest one level in practice; the bound exists so a hostile or malformed
# payload cannot turn the walk into a stack overflow.
_MAX_NESTING = 4

# Wording, and why each clause is there:
#
#   ``[[ ... ]]``      a sentinel that is not ``<system-reminder>`` and not
#                      anything the Read/Grep/Glob tools themselves emit, so the
#                      model cannot mistake the note for part of the file or for
#                      a message from its client.
#   "my-claude-code"   names the actor. Without it the natural reading is that
#                      the file itself is short or that the tool truncated, and
#                      the model then reports that as fact about the codebase.
#   "You have NOT seen ...
#    do not describe"  states the negative explicitly. Absent content produces
#                      no felt gap; the model has to be told the gap is there
#                      before it will decline to speak for it.
#   counts and span    quantified so the model can judge whether the hole
#                      matters at all, and where it is.
#   "Re-run ..."       a concrete recovery action. A warning with no remedy just
#                      makes the model hedge; this makes it able to fetch.
TRIM_MARKER_OPEN = "[[ my-claude-code TRIMMED THIS TOOL RESULT:"
_TRIM_MARKER_TEMPLATE = (
    "\n\n"
    + TRIM_MARKER_OPEN
    + " {removed} of {total} characters were removed from the middle of this"
    " {tool} result ({span}). The elision was made by the my-claude-code proxy,"
    " NOT by the {tool} tool, and NOT by the file being short. You have NOT seen"
    " the removed content: do not describe, summarise, quote, or draw any"
    " conclusion about it. Re-run {tool} over the omitted range if you need it."
    " ]]\n\n"
)

# ``Read`` renders each line as a right-aligned number, a tab, then the text, so
# the real line numbers are present in the kept text and can be quoted back
# rather than recomputed (recomputing would be wrong whenever Read was called
# with an offset).
_LINE_NUMBER = re.compile(r"^\s*(\d+)\t")


class TrimMode(StrEnum):
    """What one rule is allowed to do.

    ``OBSERVE`` is the point of the whole design: it measures a rule against
    real traffic and records what it would have removed, while the bytes on the
    wire stay exactly as the client sent them.
    """

    OFF = "off"
    OBSERVE = "observe"
    ON = "on"


TRIM_MODE_NAMES: frozenset[str] = frozenset(mode.value for mode in TrimMode)


@dataclass(frozen=True, slots=True)
class ToolResultTrimPolicy:
    """Everything this module is allowed to do, decided elsewhere.

    ``enabled`` is the master switch and beats every per-rule mode, so there is
    one place to turn the layer off with no partial states.
    """

    enabled: bool = False
    modes: Mapping[str, TrimMode] = field(default_factory=dict)
    threshold_chars: int = 0
    keep_head_chars: int = 0
    keep_tail_chars: int = 0
    protect_recent_results: int = 0

    def mode_for(self, tool: str) -> TrimMode:
        if not self.enabled:
            return TrimMode.OFF
        return self.modes.get(tool, TrimMode.OFF)

    def is_inert(self) -> bool:
        """Return whether nothing could possibly happen, so nothing is scanned.

        Checked before a single message is looked at: a disabled layer has to
        cost approximately nothing on a request that carries half a megabyte of
        tool results.
        """
        if not self.enabled:
            return True
        return all(
            self.modes.get(tool, TrimMode.OFF) is TrimMode.OFF
            for tool in TRIMMABLE_TOOL_NAMES
        )


@dataclass(frozen=True, slots=True)
class TrimOutcome:
    """One result a rule acted on, or would have acted on in ``observe``."""

    tool: str
    mode: TrimMode
    chars_before: int
    chars_after: int

    @property
    def chars_removed(self) -> int:
        return self.chars_before - self.chars_after


@dataclass(frozen=True, slots=True)
class ToolResultTrimReport:
    """What the layer did, or would have done, to one request.

    Reported rather than inferred: a rule whose effect nobody measured is a
    rule nobody can evaluate, and the point of ``observe`` is to produce this
    report without touching the wire.
    """

    outcomes: tuple[TrimOutcome, ...] = ()
    scanned: int = 0

    @property
    def applied(self) -> bool:
        """Whether any byte on the wire actually changed."""
        return any(outcome.mode is TrimMode.ON for outcome in self.outcomes)

    @property
    def chars_before(self) -> int:
        return sum(outcome.chars_before for outcome in self.outcomes)

    @property
    def chars_after(self) -> int:
        return sum(outcome.chars_after for outcome in self.outcomes)

    @property
    def chars_removed(self) -> int:
        return self.chars_before - self.chars_after

    def by_tool(self) -> dict[str, dict[str, int]]:
        """Per-tool totals, for a log row or a dashboard column."""
        totals: dict[str, dict[str, int]] = {}
        for outcome in self.outcomes:
            entry = totals.setdefault(
                outcome.tool,
                {"results": 0, "chars_before": 0, "chars_after": 0},
            )
            entry["results"] += 1
            entry["chars_before"] += outcome.chars_before
            entry["chars_after"] += outcome.chars_after
        return totals


EMPTY_TRIM_REPORT = ToolResultTrimReport()


def trim_tool_results(
    request: MessagesRequest, policy: ToolResultTrimPolicy
) -> ToolResultTrimReport:
    """Trim attributable oversized tool results in place; report either way.

    In ``observe`` the request is not touched at all -- the report is the whole
    output. In ``on`` the matching text blocks are rewritten and the same report
    describes what changed.
    """
    if policy.is_inert():
        return EMPTY_TRIM_REPORT

    names = _trimmable_tool_use_names(request)
    if not names:
        return EMPTY_TRIM_REPORT

    candidates = _candidates(request, names)
    if not candidates:
        return EMPTY_TRIM_REPORT

    # The newest results are the ones the model is reasoning about right now,
    # and they are also the cheapest to keep: an old result is re-sent on every
    # later turn, while the newest is sent once. Protecting the tail costs
    # almost nothing and removes the worst failure mode.
    protected = max(policy.protect_recent_results, 0)
    actionable = candidates[: len(candidates) - protected] if protected else candidates

    outcomes: list[TrimOutcome] = []
    for holder, tool in actionable:
        mode = policy.mode_for(tool)
        if mode is TrimMode.OFF:
            continue
        outcome = _trim_holder(holder, tool=tool, mode=mode, policy=policy)
        if outcome is not None:
            outcomes.append(outcome)
    return ToolResultTrimReport(tuple(outcomes), scanned=len(candidates))


@dataclass(slots=True)
class _TextHolder:
    """One editable piece of text inside a tool result, and how to write it."""

    text: str
    container: Any
    key: str | int | None

    def write(self, value: str) -> None:
        if self.key is None:
            self.container.content = value
        elif isinstance(self.key, int):
            self.container[self.key] = value
        else:
            self.container[self.key] = value


def _trimmable_tool_use_names(request: MessagesRequest) -> dict[str, str]:
    """Map ``tool_use_id`` to tool name, for trimmable tools only.

    Every ``tool_use`` is recorded, not only the trimmable ones, so that an id
    claimed by both ``Read`` and ``Bash`` is seen as the ambiguity it is. An id
    with two names is dropped rather than guessed at: attribution that is not
    certain is attribution this module refuses to act on.
    """
    names: dict[str, str] = {}
    conflicting: set[str] = set()
    for message in request.messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for block in content:
            if get_block_type(block) != "tool_use":
                continue
            name = get_block_attr(block, "name")
            if not isinstance(name, str) or not name:
                continue
            block_id = get_block_attr(block, "id")
            if not isinstance(block_id, str) or not block_id:
                continue
            previous = names.get(block_id)
            if previous is not None and previous != name:
                conflicting.add(block_id)
            names[block_id] = name
    return {
        block_id: name
        for block_id, name in names.items()
        if name in TRIMMABLE_TOOL_NAMES and block_id not in conflicting
    }


def _candidates(
    request: MessagesRequest, names: dict[str, str]
) -> list[tuple[_TextHolder, str]]:
    """Collect editable text from attributable, non-error tool results, in order."""
    found: list[tuple[_TextHolder, str]] = []
    for message in request.messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for block in content:
            if get_block_type(block) != "tool_result":
                continue
            # An error is the shortest and most load-bearing thing a tool ever
            # returns. Cutting one would hide the reason a step failed.
            if bool(get_block_attr(block, "is_error", False)):
                continue
            tool_use_id = get_block_attr(block, "tool_use_id")
            tool = names.get(tool_use_id) if isinstance(tool_use_id, str) else None
            if tool is None:
                continue
            holder = _largest_text(get_block_attr(block, "content"), block)
            if holder is not None:
                found.append((holder, tool))
    return found


def _largest_text(content: Any, block: Any, *, depth: int = 0) -> _TextHolder | None:
    """Return the biggest editable text inside a tool result, or None.

    Only ``str`` content and ``{"type": "text"}`` items are editable. Images,
    documents and any other block shape are skipped entirely, so a screenshot
    returned by a tool is never touched.
    """
    if depth > _MAX_NESTING:
        return None
    if isinstance(content, str):
        return _TextHolder(content, block, None)
    if isinstance(content, dict):
        if content.get("type") != "text":
            return None
        text = content.get("text")
        if not isinstance(text, str):
            return None
        return _TextHolder(text, content, "text")
    if not isinstance(content, list):
        return None
    best: _TextHolder | None = None
    for index, item in enumerate(content):
        if isinstance(item, str):
            candidate = _TextHolder(item, content, index)
        else:
            candidate = _largest_text(item, block, depth=depth + 1)
        if candidate is None:
            continue
        if best is None or len(candidate.text) > len(best.text):
            best = candidate
    return best


def _trim_holder(
    holder: _TextHolder,
    *,
    tool: str,
    mode: TrimMode,
    policy: ToolResultTrimPolicy,
) -> TrimOutcome | None:
    trimmed = trim_text(
        holder.text,
        tool=tool,
        threshold_chars=policy.threshold_chars,
        keep_head_chars=policy.keep_head_chars,
        keep_tail_chars=policy.keep_tail_chars,
    )
    if trimmed is None:
        return None
    if mode is TrimMode.ON:
        holder.write(trimmed)
    return TrimOutcome(
        tool=tool,
        mode=mode,
        chars_before=len(holder.text),
        chars_after=len(trimmed),
    )


def trim_text(
    text: str,
    *,
    tool: str,
    threshold_chars: int,
    keep_head_chars: int,
    keep_tail_chars: int,
) -> str | None:
    """Return the trimmed text, or None when this text must be left alone.

    The head and the tail are kept because that is where the structure the model
    needs to act lives: the path and line numbers a ``Read`` opens with, the
    last matches a ``Grep`` found. The middle is the only part of a long body
    that can be described by a marker without losing the ability to navigate
    what remains.
    """
    total = len(text)
    if threshold_chars <= 0 or total <= threshold_chars:
        return None

    head_end = _snap_forward(text, max(keep_head_chars, 0))
    tail_start = _snap_back(text, total - max(keep_tail_chars, 0))
    if tail_start <= head_end:
        return None

    head = text[:head_end]
    tail = text[tail_start:]
    marker = _TRIM_MARKER_TEMPLATE.format(
        removed=tail_start - head_end,
        total=total,
        tool=tool,
        span=_describe_span(head, tail, head_end, tail_start),
    )
    result = f"{head}{marker}{tail}"
    # Refuse a "trim" that does not actually shrink the request. A marker longer
    # than the hole it explains is pure cost, and the guard also makes a
    # misconfigured threshold harmless rather than inflationary.
    if len(result) >= total:
        return None
    return result


def _snap_forward(text: str, index: int) -> int:
    """Move a cut forward to the end of the line it lands in.

    ``Grep`` and ``Glob`` emit one path (or ``path:line:match``) per line. A cut
    mid-line would hand the model a truncated path that looks like a real one,
    which is worse than a smaller saving.
    """
    if index <= 0:
        return 0
    newline = text.find("\n", index)
    if newline == -1:
        return index
    return newline + 1


def _snap_back(text: str, index: int) -> int:
    if index >= len(text):
        return len(text)
    newline = text.rfind("\n", 0, max(index, 0))
    if newline == -1:
        return max(index, 0)
    return newline + 1


def _describe_span(head: str, tail: str, head_end: int, tail_start: int) -> str:
    """Say which lines are missing when the text says so, else which characters."""
    first_gone = _line_number_after(head)
    last_gone = _line_number_before(tail)
    if first_gone is not None and last_gone is not None and last_gone > first_gone:
        return f"lines {first_gone} to {last_gone}"
    return f"characters {head_end} to {tail_start}"


def _line_number_after(head: str) -> int | None:
    """Number of the first removed line, read from the last kept line."""
    lines = head.splitlines()
    while lines:
        match = _LINE_NUMBER.match(lines.pop())
        if match is not None:
            return int(match.group(1)) + 1
    return None


def _line_number_before(tail: str) -> int | None:
    """Number of the last removed line, read from the first kept line."""
    for line in tail.splitlines():
        match = _LINE_NUMBER.match(line)
        if match is not None:
            return int(match.group(1)) - 1
    return None
