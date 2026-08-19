"""Provider-owned reasoning translations for OpenAI-compatible APIs."""

from dataclasses import dataclass
from typing import Any, Protocol

from my_claude_code.core.reasoning import (
    ReasoningControl,
    ReasoningEffort,
    ReasoningPolicy,
)

EffortValues = tuple[tuple[ReasoningEffort, str], ...]


class ReasoningEncoder(Protocol):
    """Translate provider-neutral reasoning intent into one wire shape."""

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None: ...


@dataclass(frozen=True, slots=True)
class NoReasoning:
    """Leave reasoning computation entirely to the upstream provider."""

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        return


@dataclass(frozen=True, slots=True)
class NamedEffortReasoning:
    """Encode a provider's documented named-effort vocabulary."""

    efforts: EffortValues
    disabled_value: str | bool | None = None
    enabled_value: str | bool | None = None
    field: str = "reasoning_effort"
    budget_field: str | None = None
    use_extra_body: bool = False

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        target = _extra_body(body) if self.use_extra_body else body
        if policy.control is ReasoningControl.OFF:
            if self.disabled_value is not None:
                target[self.field] = self.disabled_value
            return

        if policy.budget_tokens is not None and self.budget_field is not None:
            target[self.budget_field] = policy.budget_tokens
            return

        effort = dict(self.efforts).get(policy.effort)
        if effort is not None:
            target[self.field] = effort
            return

        if policy.control is ReasoningControl.ON and self.enabled_value is not None:
            target[self.field] = self.enabled_value


@dataclass(frozen=True, slots=True)
class ReasoningObject:
    """Encode gateways that accept a top-level ``reasoning`` object."""

    efforts: EffortValues
    supports_budget: bool = True

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if policy.control is ReasoningControl.OFF:
            _extra_body(body)["reasoning"] = {"enabled": False}
            return

        reasoning: dict[str, Any] = {}
        if policy.budget_tokens is not None and self.supports_budget:
            reasoning["max_tokens"] = policy.budget_tokens
        elif effort := dict(self.efforts).get(policy.effort):
            reasoning["effort"] = effort
        elif policy.control is ReasoningControl.ON:
            reasoning["enabled"] = True

        if reasoning:
            _extra_body(body)["reasoning"] = reasoning


@dataclass(frozen=True, slots=True)
class ThinkingObjectReasoning:
    """Encode providers with an enabled/disabled ``thinking`` object."""

    enabled: dict[str, Any]
    disabled: dict[str, Any]

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if policy.control is ReasoningControl.OFF:
            _extra_body(body)["thinking"] = dict(self.disabled)
        elif policy.requests_reasoning:
            _extra_body(body)["thinking"] = dict(self.enabled)


@dataclass(frozen=True, slots=True)
class EffortOrThinkingBudgetReasoning:
    """Encode providers whose named-effort and numeric-budget fields are
    mutually exclusive on the wire.

    Some OpenAI-compatible gateways expose two reasoning knobs that the
    vendor's own API rejects together in one request: a string enum
    ``reasoning_effort`` field, and a separate ``thinking`` object carrying
    an exact ``budget_tokens`` integer. This encoder picks exactly one shape
    per request and never emits both:

    - An explicit client ``budget_tokens`` always wins and is sent as
      ``{"type": "enabled", "budget_tokens": N}`` under ``thinking_field``,
      clamped up to ``min_budget_tokens`` (never down, per the vendor's
      documented floor). ``field`` is omitted entirely.
    - Otherwise a named effort is translated through ``efforts`` into
      ``field`` (typically ``reasoning_effort``). ``thinking_field`` is
      omitted entirely.
    - With no effort or budget but reasoning explicitly ``ON``,
      ``enabled_value`` (if set) is sent through ``field``.
    - Reasoning explicitly ``OFF`` sends ``{"type": "disabled"}`` under
      ``thinking_field`` and never sets ``field``.
    """

    efforts: EffortValues
    field: str = "reasoning_effort"
    thinking_field: str = "thinking"
    enabled_value: str | None = None
    min_budget_tokens: int = 1024

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if policy.control is ReasoningControl.OFF:
            _extra_body(body)[self.thinking_field] = {"type": "disabled"}
            return

        if policy.budget_tokens is not None:
            budget = max(self.min_budget_tokens, policy.budget_tokens)
            _extra_body(body)[self.thinking_field] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
            return

        effort = dict(self.efforts).get(policy.effort)
        if effort is not None:
            body[self.field] = effort
            return

        if policy.control is ReasoningControl.ON and self.enabled_value is not None:
            body[self.field] = self.enabled_value


@dataclass(frozen=True, slots=True)
class ChatTemplateReasoning:
    """Encode a provider-wide chat-template boolean without model guessing."""

    field: str = "thinking"

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if not policy.requests_reasoning and policy.control is not ReasoningControl.OFF:
            return
        kwargs = _nested_dict(_extra_body(body), "chat_template_kwargs")
        kwargs[self.field] = policy.control is not ReasoningControl.OFF


@dataclass(frozen=True, slots=True)
class LlamaCppReasoning:
    """Encode llama.cpp's per-request numeric thinking budget."""

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if policy.control is ReasoningControl.OFF:
            _extra_body(body)["thinking_budget_tokens"] = 0
        elif (budget := policy.numeric_budget_tokens) is not None:
            _extra_body(body)["thinking_budget_tokens"] = budget


@dataclass(frozen=True, slots=True)
class SplitReasoningOutput:
    """Request separate reasoning output where compute is not controllable."""

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        _extra_body(body)["reasoning_split"] = True


def _extra_body(body: dict[str, Any]) -> dict[str, Any]:
    value = body.setdefault("extra_body", {})
    if not isinstance(value, dict):
        raise TypeError("OpenAI extra_body must be an object.")
    return value


def _nested_dict(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.setdefault(key, {})
    if not isinstance(value, dict):
        raise TypeError(f"{key} must be an object.")
    return value


NO_REASONING = NoReasoning()
LLAMACPP_REASONING = LlamaCppReasoning()
SPLIT_REASONING_OUTPUT = SplitReasoningOutput()
