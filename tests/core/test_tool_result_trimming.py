"""Tool-result trimming: what it refuses to do matters more than what it does.

This layer changes what the model sees, so most of what is pinned here is a
refusal -- default off, ambiguous attribution, error results, Bash, images, and
any trim that would not actually shrink the request.
"""

import json
from typing import Any

import pytest

from my_claude_code.api.handlers.messages import (
    MessagesHandler,
    _tool_result_trim_policy,
)
from my_claude_code.application.ports import ProviderPort
from my_claude_code.config.constants import TRIM_MODE_NAMES as CONFIG_TRIM_MODE_NAMES
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    TRIM_MARKER_OPEN,
    TRIM_MODE_NAMES,
    TRIMMABLE_TOOL_NAMES,
    ContentBlockToolResult,
    ContentBlockToolUse,
    MessagesRequest,
    ToolResultTrimPolicy,
    TrimMode,
    trim_tool_results,
)

BIG = 60_000


def _numbered(lines: int, *, start: int = 1, width: int = 70) -> str:
    return "".join(f"{n:6d}\t{'x' * width}\n" for n in range(start, start + lines))


def _policy(
    *,
    enabled: bool = True,
    read: TrimMode = TrimMode.ON,
    grep: TrimMode = TrimMode.OFF,
    glob: TrimMode = TrimMode.OFF,
    threshold: int = 20_000,
    head: int = 4_000,
    tail: int = 4_000,
    protect: int = 0,
) -> ToolResultTrimPolicy:
    return ToolResultTrimPolicy(
        enabled=enabled,
        modes={"Read": read, "Grep": grep, "Glob": glob},
        threshold_chars=threshold,
        keep_head_chars=head,
        keep_tail_chars=tail,
        protect_recent_results=protect,
    )


def _request(
    *,
    tool: str = "Read",
    tool_id: str = "toolu_1",
    result_id: str | None = None,
    body: str | list | dict | None = None,
    is_error: bool = False,
    extra_pairs: int = 0,
) -> MessagesRequest:
    """One assistant tool_use plus the user tool_result that answers it."""
    content = body if body is not None else _numbered(BIG // 78)
    messages: list[dict] = [{"role": "user", "content": "go"}]
    for index in range(extra_pairs):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_pad{index}",
                        "name": tool,
                        "input": {},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"toolu_pad{index}",
                        "content": _numbered(BIG // 78),
                    }
                ],
            }
        )
    messages.append(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": tool, "input": {}}],
        }
    )
    result: dict = {
        "type": "tool_result",
        "tool_use_id": result_id if result_id is not None else tool_id,
        "content": content,
    }
    if is_error:
        result["is_error"] = True
    messages.append({"role": "user", "content": [result]})
    return MessagesRequest.model_validate(
        {"model": "anthropic/claude", "messages": messages}
    )


def _wire(request: MessagesRequest) -> str:
    return request.model_dump_json()


def _tool_result_blocks(request: MessagesRequest) -> list[ContentBlockToolResult]:
    """Every tool_result block in the request, narrowed for the type checker."""
    blocks: list[ContentBlockToolResult] = []
    for message in request.messages:
        content = message.content
        if not isinstance(content, list):
            continue
        blocks.extend(
            block for block in content if isinstance(block, ContentBlockToolResult)
        )
    return blocks


def _result_text(request: MessagesRequest) -> str:
    body = _tool_result_blocks(request)[-1].content
    assert isinstance(body, str)
    return body


def _result_items(request: MessagesRequest) -> list[Any]:
    body = _tool_result_blocks(request)[-1].content
    assert isinstance(body, list)
    return body


def _settings(**env: str) -> Settings:
    # Env values arrive as strings and the model coerces them, which a
    # precisely-typed kwargs dict cannot express -- the same shape
    # tests/config/test_limit_bounds.py uses.
    kwargs: dict[str, Any] = {"_env_file": None, **env}
    return Settings(**kwargs)


# --------------------------------------------------------------------------
# The most important test in the change.
# --------------------------------------------------------------------------


def test_default_settings_leave_a_large_request_byte_identical() -> None:
    """A fresh install must behave exactly as the release before this layer.

    Byte-identity rather than "looks the same": the whole point of the master
    switch is that nothing downstream can tell the layer was compiled in.
    """
    settings = _settings()
    policy = _tool_result_trim_policy(settings)
    request = _request(extra_pairs=3)
    before = _wire(request)

    report = trim_tool_results(request, policy)

    assert policy.is_inert()
    assert report.outcomes == ()
    assert _wire(request) == before


@pytest.mark.parametrize("tool", TRIMMABLE_TOOL_NAMES)
def test_every_rule_is_off_by_default(tool: str) -> None:
    """Turning a rule on by default would silently change every install."""
    settings = _settings()
    assert settings.enable_tool_result_trimming is False
    assert _tool_result_trim_policy(settings).mode_for(tool) is TrimMode.OFF


def test_the_master_switch_beats_a_rule_that_is_on() -> None:
    request = _request()
    before = _wire(request)

    report = trim_tool_results(request, _policy(enabled=False, read=TrimMode.ON))

    assert report.outcomes == ()
    assert _wire(request) == before


# --------------------------------------------------------------------------
# Observe
# --------------------------------------------------------------------------


def test_observe_records_the_saving_without_touching_the_wire() -> None:
    request = _request()
    before = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.OBSERVE))

    assert _wire(request) == before
    assert report.applied is False
    assert len(report.outcomes) == 1
    assert report.outcomes[0].mode is TrimMode.OBSERVE
    assert report.chars_removed > 0
    assert report.by_tool()["Read"]["results"] == 1


def test_observe_and_on_measure_the_same_saving() -> None:
    """Observe is only useful if its numbers are the numbers On would produce."""
    observed = trim_tool_results(_request(), _policy(read=TrimMode.OBSERVE))
    applied = trim_tool_results(_request(), _policy(read=TrimMode.ON))

    assert observed.chars_before == applied.chars_before
    assert observed.chars_after == applied.chars_after


def test_a_rule_in_observe_does_not_enable_another_rule() -> None:
    request = _request(tool="Grep")
    before = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.ON, grep=TrimMode.OFF))

    assert report.outcomes == ()
    assert _wire(request) == before


# --------------------------------------------------------------------------
# The marker
# --------------------------------------------------------------------------


def test_a_trimmed_result_carries_the_marker() -> None:
    request = _request()

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    text = _result_text(request)
    assert report.applied is True
    assert TRIM_MARKER_OPEN in text
    assert "my-claude-code TRIMMED THIS TOOL RESULT:" in text
    assert "characters were removed from the middle of this Read result" in text
    assert "NOT by the Read tool" in text
    assert "You have NOT seen the removed content" in text
    assert "Re-run Read over the omitted range if you need it." in text


def test_the_marker_quotes_the_real_line_numbers_it_removed() -> None:
    """Read renders line numbers, so the gap can be named rather than guessed."""
    request = _request(body=_numbered(3_000, start=1))

    trim_tool_results(request, _policy(read=TrimMode.ON))

    text = _result_text(request)
    assert "(lines " in text
    span = text.split("(lines ", 1)[1].split(")", 1)[0]
    first_gone, last_gone = (int(part) for part in span.split(" to "))
    assert 1 < first_gone < last_gone < 3_000
    # The named range is genuinely absent and its neighbours are genuinely there.
    assert f"\n{first_gone - 1:6d}\t" in text
    assert f"\n{last_gone + 1:6d}\t" in text
    assert f"\n{first_gone:6d}\t" not in text
    assert f"\n{last_gone:6d}\t" not in text


def test_a_result_without_line_numbers_names_the_character_range() -> None:
    request = _request(tool="Glob", body="\n".join(f"src/f{n}.py" for n in range(6000)))

    trim_tool_results(request, _policy(read=TrimMode.OFF, glob=TrimMode.ON))

    text = _result_text(request)
    assert "(characters " in text


def test_the_marker_reports_the_amount_it_removed() -> None:
    request = _request()

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    text = _result_text(request)
    removed = int(text.split(TRIM_MARKER_OPEN)[1].split(" of ", 1)[0].strip())
    total = int(text.split(" of ", 1)[1].split(" characters", 1)[0])
    head, rest = text.split("\n\n" + TRIM_MARKER_OPEN, 1)
    kept_tail = rest.split("]]\n\n", 1)[1]
    assert total == report.chars_before
    assert removed == total - (len(head) + len(kept_tail))
    assert removed > 0


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_result_at_or_below_the_threshold_is_untouched() -> None:
    request = _request(body=_numbered(10))
    before = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    assert report.outcomes == ()
    assert _wire(request) == before


def test_an_error_result_is_never_trimmed() -> None:
    """Error text is the shortest, most load-bearing thing a tool returns."""
    request = _request(is_error=True)
    before = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    assert report.outcomes == ()
    assert _wire(request) == before


def test_an_unattributable_result_is_untouched() -> None:
    """No tool_use names this id, so nothing knows which tool produced it."""
    request = _request(result_id="toolu_orphan")
    before = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    assert report.outcomes == ()
    assert _wire(request) == before


def test_an_id_claimed_by_two_different_tools_is_untouched() -> None:
    """Ambiguous attribution is refused rather than resolved by guessing."""
    request = _request()
    request.messages.insert(
        1,
        request.messages[-2].model_copy(deep=True),
    )
    duplicate = request.messages[1].content
    assert isinstance(duplicate, list)
    assert isinstance(duplicate[0], ContentBlockToolUse)
    duplicate[0].name = "Bash"
    before = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    assert report.outcomes == ()
    assert _wire(request) == before


def test_bash_results_are_untouched_however_large() -> None:
    """Bash belongs to client-side hooks; two compressors make neither countable."""
    request = _request(tool="Bash")
    before = _wire(request)

    report = trim_tool_results(
        request,
        _policy(read=TrimMode.ON, grep=TrimMode.ON, glob=TrimMode.ON),
    )

    assert report.outcomes == ()
    assert _wire(request) == before


def test_an_image_inside_a_tool_result_is_untouched() -> None:
    """A Read of a PNG returns pixels, and pixels have no middle to elide."""
    request = _request(
        body=[
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "A" * BIG,
                },
            }
        ]
    )
    before = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    assert report.outcomes == ()
    assert _wire(request) == before


def test_a_trim_that_would_not_shrink_the_body_is_refused() -> None:
    """A marker longer than the hole it explains is pure cost."""
    body = _numbered(400)
    request = _request(body=body)
    before = _wire(request)

    report = trim_tool_results(
        request,
        _policy(read=TrimMode.ON, threshold=100, head=len(body), tail=len(body)),
    )

    assert report.outcomes == ()
    assert _wire(request) == before


def test_the_newest_results_are_protected() -> None:
    request = _request(extra_pairs=2)
    bodies_before = [block.content for block in _tool_result_blocks(request)]

    report = trim_tool_results(request, _policy(read=TrimMode.ON, protect=2))

    bodies_after = [block.content for block in _tool_result_blocks(request)]
    assert len(bodies_before) == 3
    assert len(report.outcomes) == 1
    assert bodies_after[1:] == bodies_before[1:]
    assert bodies_after[0] != bodies_before[0]


def test_protecting_zero_leaves_every_result_trimmable() -> None:
    request = _request(extra_pairs=2)

    report = trim_tool_results(request, _policy(read=TrimMode.ON, protect=0))

    assert len(report.outcomes) == 3


# --------------------------------------------------------------------------
# Shape and structure
# --------------------------------------------------------------------------


def test_a_cut_never_lands_mid_line() -> None:
    """A half path looks exactly like a real one, which is worse than no saving."""
    paths = "\n".join(f"src/some/deep/module_{n}.py" for n in range(6000))
    request = _request(tool="Grep", body=paths)

    trim_tool_results(request, _policy(read=TrimMode.OFF, grep=TrimMode.ON))

    text = _result_text(request)
    head, tail = text.split(TRIM_MARKER_OPEN, 1)
    assert head.endswith("\n")
    kept_tail = tail.split("]]\n\n", 1)[1]
    for line in head.splitlines() + kept_tail.splitlines():
        assert line == "" or line.startswith("src/some/deep/module_")
        assert line == "" or line.endswith(".py")


def test_a_text_block_list_body_is_trimmed_in_place() -> None:
    request = _request(
        body=[{"type": "text", "text": _numbered(BIG // 78)}],
    )

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    block = _result_items(request)[0]
    assert report.applied is True
    assert block["type"] == "text"
    assert TRIM_MARKER_OPEN in block["text"]


def test_the_trimmed_request_still_validates_as_an_anthropic_request() -> None:
    request = _request(extra_pairs=1)

    trim_tool_results(request, _policy(read=TrimMode.ON))

    round_tripped = MessagesRequest.model_validate(json.loads(_wire(request)))
    assert _wire(round_tripped) == _wire(request)
    assert _tool_result_blocks(round_tripped)[-1].tool_use_id == "toolu_1"


def test_the_transform_is_deterministic() -> None:
    """Prompt caching only survives if the same result trims to the same bytes."""
    first = _request()
    second = _request()

    trim_tool_results(first, _policy(read=TrimMode.ON))
    trim_tool_results(second, _policy(read=TrimMode.ON))

    assert _wire(first) == _wire(second)


def test_trimming_an_already_trimmed_result_is_a_fixed_point() -> None:
    """Turn N+1 resends turn N's trimmed bytes; it must not trim them again."""
    request = _request()
    trim_tool_results(request, _policy(read=TrimMode.ON))
    once = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    assert report.outcomes == ()
    assert _wire(request) == once


# --------------------------------------------------------------------------
# Configuration contracts
# --------------------------------------------------------------------------


def test_config_mirrors_the_trim_modes_it_cannot_import() -> None:
    """`config` is a leaf package, so it repeats the mode names by hand."""
    assert CONFIG_TRIM_MODE_NAMES == TRIM_MODE_NAMES
    assert {mode.value for mode in TrimMode} == TRIM_MODE_NAMES


def test_the_policy_names_exactly_the_tools_the_transform_knows() -> None:
    policy = _tool_result_trim_policy(_settings())
    assert set(policy.modes) == set(TRIMMABLE_TOOL_NAMES)
    assert "Bash" not in TRIMMABLE_TOOL_NAMES


@pytest.mark.parametrize(
    "alias",
    ("TOOL_RESULT_TRIM_READ", "TOOL_RESULT_TRIM_GREP", "TOOL_RESULT_TRIM_GLOB"),
)
def test_an_unknown_mode_is_rejected_rather_than_guessed_at(alias: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Unknown trim mode"):
        _settings(**{alias: "aggressive"})


@pytest.mark.parametrize(
    "alias",
    ("TOOL_RESULT_TRIM_READ", "TOOL_RESULT_TRIM_GREP", "TOOL_RESULT_TRIM_GLOB"),
)
def test_a_cleared_mode_falls_back_to_off(alias: str) -> None:
    """The admin UI writes `KEY=` for a cleared field."""
    settings = _settings(**{alias: ""})
    assert getattr(settings, alias.lower()) == "off"


def test_env_example_documents_every_trim_setting() -> None:
    from pathlib import Path

    from my_claude_code.config.admin.manifest import FIELDS

    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text("utf-8")
    trim_keys = [f.key for f in FIELDS if "TOOL_RESULT_TRIM" in f.key]

    assert len(trim_keys) == 8
    for key in trim_keys:
        assert f"\n{key}=" in text, f"{key} is missing from .env.example"


# --------------------------------------------------------------------------
# The switches themselves, tested directly.
#
# The layer has two independent off-ramps -- `is_inert` short-circuits the walk
# and `mode_for` gates each rule -- and each one alone is enough to produce the
# right bytes. That mutual masking is what makes them worth pinning separately:
# breaking either in isolation is invisible through the transform's output.
# --------------------------------------------------------------------------


def test_an_inert_policy_never_walks_the_request() -> None:
    """The disabled path must cost nothing on a half-megabyte prompt.

    Asserted by identity: the shared empty report is only ever returned by the
    early exit, so getting it back proves no message was scanned.
    """
    from my_claude_code.core.anthropic.tool_result_trimming import EMPTY_TRIM_REPORT

    request = _request(extra_pairs=3)

    report = trim_tool_results(request, _policy(enabled=False, read=TrimMode.ON))

    assert report is EMPTY_TRIM_REPORT
    assert report.scanned == 0


def test_the_master_switch_forces_every_mode_off() -> None:
    policy = _policy(
        enabled=False, read=TrimMode.ON, grep=TrimMode.ON, glob=TrimMode.OBSERVE
    )

    assert [policy.mode_for(tool) for tool in TRIMMABLE_TOOL_NAMES] == [
        TrimMode.OFF,
        TrimMode.OFF,
        TrimMode.OFF,
    ]


def test_a_policy_with_every_rule_off_is_inert_even_when_enabled() -> None:
    assert _policy(enabled=True, read=TrimMode.OFF).is_inert()
    assert not _policy(enabled=True, read=TrimMode.OBSERVE).is_inert()
    assert not _policy(enabled=True, read=TrimMode.ON).is_inert()
    assert _policy(enabled=False, read=TrimMode.ON).is_inert()


def test_a_trim_whose_marker_costs_more_than_the_hole_is_refused() -> None:
    """Head and tail do not overlap here; the marker alone makes it not worth it.

    A separate case from the overlapping-window one, because the two refusals
    are separate guards and each has to be reachable on its own.
    """
    body = "abc\n" * 250
    request = _request(body=body)
    before = _wire(request)

    report = trim_tool_results(
        request,
        _policy(read=TrimMode.ON, threshold=100, head=460, tail=460),
    )

    assert len(body) == 1_000
    assert report.outcomes == ()
    assert _wire(request) == before


def test_only_a_block_that_declares_itself_text_is_edited() -> None:
    """The block type is what is trusted, not the presence of a `text` key."""
    request = _request(
        body=[{"type": "image", "text": "x" * BIG, "source": {"type": "base64"}}]
    )
    before = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    assert report.outcomes == ()
    assert _wire(request) == before


def test_a_text_block_whose_text_is_not_a_string_is_left_alone() -> None:
    """A lenient client can send anything; a non-string body is not editable."""
    request = _request(body=[{"type": "text", "text": ["x" * 70] * 30_000}])
    before = _wire(request)

    report = trim_tool_results(request, _policy(read=TrimMode.ON))

    assert report.outcomes == ()
    assert _wire(request) == before


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def _handler(**env: str) -> MessagesHandler:
    """A handler with a resolver that is never reached: no provider is called."""

    def _unused_resolver(model_ref: str) -> ProviderPort:
        raise AssertionError(f"the trim layer must not resolve {model_ref!r}")

    return MessagesHandler(_settings(**env), provider_resolver=_unused_resolver)


def _captured_trim_events(request: MessagesRequest, **env: str) -> list[dict[str, Any]]:
    from loguru import logger

    rows: list[dict[str, Any]] = []
    sink = logger.add(
        lambda message: rows.append(dict(message.record["extra"])),
        level="DEBUG",
        filter=lambda record: "trace_payload" in record["extra"],
    )
    try:
        _handler(**env)._trim_tool_results(request, request_id="req_1")
    finally:
        logger.remove(sink)
    return [
        row["trace_payload"]
        for row in rows
        if row["trace_payload"].get("event") == "my_claude_code.api.tool_result_trim"
    ]


def test_a_trim_is_recorded_with_before_and_after_byte_counts() -> None:
    """Measured savings, not a vendor percentage: the numbers come from the run."""
    request = _request()

    events = _captured_trim_events(
        request,
        ENABLE_TOOL_RESULT_TRIMMING="true",
        TOOL_RESULT_TRIM_READ="on",
        TOOL_RESULT_TRIM_PROTECT_RECENT_RESULTS="0",
    )

    assert len(events) == 1
    event = events[0]
    assert event["applied"] is True
    assert event["results_matched"] == 1
    assert event["chars_before"] > event["chars_after"] > 0
    assert event["chars_removed"] == event["chars_before"] - event["chars_after"]
    assert event["by_tool"]["Read"]["results"] == 1


def test_observe_is_recorded_even_though_nothing_changed() -> None:
    request = _request()
    before = _wire(request)

    events = _captured_trim_events(
        request,
        ENABLE_TOOL_RESULT_TRIMMING="true",
        TOOL_RESULT_TRIM_READ="observe",
        TOOL_RESULT_TRIM_PROTECT_RECENT_RESULTS="0",
    )

    assert _wire(request) == before
    assert len(events) == 1
    assert events[0]["applied"] is False
    assert events[0]["chars_removed"] > 0


def test_a_disabled_layer_records_nothing() -> None:
    assert _captured_trim_events(_request()) == []
