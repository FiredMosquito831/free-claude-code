"""Behavioural tests for EffortOrThinkingBudgetReasoning (the Fireworks wire
shape: ``reasoning_effort`` and ``thinking`` are mutually exclusive)."""

import pytest

from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.reasoning import (
    EffortOrThinkingBudgetReasoning,
)

_FIREWORKS_EFFORTS = (
    (ReasoningEffort.MINIMAL, "low"),
    (ReasoningEffort.LOW, "low"),
    (ReasoningEffort.MEDIUM, "medium"),
    (ReasoningEffort.HIGH, "high"),
    (ReasoningEffort.XHIGH, "high"),
    (ReasoningEffort.MAX, "high"),
)


def _encoder() -> EffortOrThinkingBudgetReasoning:
    return EffortOrThinkingBudgetReasoning(_FIREWORKS_EFFORTS, enabled_value="high")


def test_effort_high_sends_reasoning_effort_high_and_no_thinking() -> None:
    body: dict = {}

    _encoder().encode(body, ReasoningPolicy.on(effort=ReasoningEffort.HIGH))

    assert body == {"reasoning_effort": "high"}
    assert "thinking" not in body
    assert "thinking" not in body.get("extra_body", {})


@pytest.mark.parametrize("effort", [ReasoningEffort.XHIGH, ReasoningEffort.MAX])
def test_effort_xhigh_and_max_clamp_to_high(effort: ReasoningEffort) -> None:
    body: dict = {}

    _encoder().encode(body, ReasoningPolicy.on(effort=effort))

    assert body == {"reasoning_effort": "high"}
    assert "thinking" not in body.get("extra_body", {})


@pytest.mark.parametrize("effort", [ReasoningEffort.MINIMAL, ReasoningEffort.LOW])
def test_effort_minimal_and_low_map_to_low(effort: ReasoningEffort) -> None:
    body: dict = {}

    _encoder().encode(body, ReasoningPolicy.on(effort=effort))

    assert body == {"reasoning_effort": "low"}


def test_never_emits_undocumented_effort_strings() -> None:
    """Fireworks docs confirm only low/medium/high; xhigh/max/minimal/none
    must never reach the wire in the reasoning_effort field."""
    documented = {"low", "medium", "high"}

    for effort in ReasoningEffort:
        body: dict = {}
        _encoder().encode(body, ReasoningPolicy.on(effort=effort))
        if "reasoning_effort" in body:
            assert body["reasoning_effort"] in documented


def test_explicit_budget_sends_thinking_object_and_omits_reasoning_effort() -> None:
    body: dict = {}

    _encoder().encode(body, ReasoningPolicy.on(budget_tokens=8192))

    assert body == {
        "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 8192}}
    }
    assert "reasoning_effort" not in body


def test_explicit_budget_below_floor_is_clamped_to_1024() -> None:
    body: dict = {}

    _encoder().encode(body, ReasoningPolicy.on(budget_tokens=512))

    assert body["extra_body"]["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "reasoning_effort" not in body


def test_reasoning_off_sends_thinking_disabled_and_no_reasoning_effort() -> None:
    body: dict = {}

    _encoder().encode(body, ReasoningPolicy.off())

    assert body == {"extra_body": {"thinking": {"type": "disabled"}}}
    assert "reasoning_effort" not in body


def test_enabled_with_no_effort_or_budget_uses_enabled_value() -> None:
    body: dict = {}

    _encoder().encode(body, ReasoningPolicy.on())

    assert body == {"reasoning_effort": "high"}
    assert "thinking" not in body.get("extra_body", {})


def test_fireworks_profile_wires_the_effort_or_thinking_budget_encoder() -> None:
    """Guard the actual production wiring, not just a hand-copied efforts
    table -- catches a regression to NamedEffortReasoning / budget_field or
    a widened efforts map in profiles.py itself."""
    encoder = OPENAI_CHAT_PROFILES["fireworks"].reasoning
    assert isinstance(encoder, EffortOrThinkingBudgetReasoning)

    body: dict = {}
    encoder.encode(body, ReasoningPolicy.on(effort=ReasoningEffort.XHIGH))
    assert body == {"reasoning_effort": "high"}

    body = {}
    encoder.encode(body, ReasoningPolicy.on(budget_tokens=8192))
    assert body == {
        "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 8192}}
    }
    assert "reasoning_effort" not in body


@pytest.mark.parametrize(
    "policy",
    [
        ReasoningPolicy.provider_default(),
        ReasoningPolicy.off(),
        ReasoningPolicy.on(),
        ReasoningPolicy.on(effort=ReasoningEffort.MINIMAL),
        ReasoningPolicy.on(effort=ReasoningEffort.LOW),
        ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        ReasoningPolicy.on(effort=ReasoningEffort.XHIGH),
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        ReasoningPolicy.on(budget_tokens=1),
        ReasoningPolicy.on(budget_tokens=1024),
        ReasoningPolicy.on(budget_tokens=8192),
    ],
)
def test_reasoning_effort_and_thinking_never_coexist(policy: ReasoningPolicy) -> None:
    body: dict = {}

    _encoder().encode(body, policy)

    has_effort = "reasoning_effort" in body
    has_thinking = "thinking" in body.get("extra_body", {})
    assert not (has_effort and has_thinking)
