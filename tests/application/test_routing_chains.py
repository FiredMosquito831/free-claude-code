"""Fallback-chain and vision-adapter routing contracts."""

import pytest

from free_claude_code.application.routing import ModelRouter, RouteDiversion
from free_claude_code.config.reasoning import ReasoningPreference
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic.models import MessagesRequest

_IMAGE_BLOCK: dict[str, object] = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
}


@pytest.fixture
def settings() -> Settings:
    settings = Settings()
    settings.model = "nvidia_nim/fallback-model"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    settings.model_fallbacks = None
    settings.model_fable_fallbacks = None
    settings.model_opus_fallbacks = None
    settings.model_sonnet_fallbacks = None
    settings.model_haiku_fallbacks = None
    settings.model_vision = None
    settings.reasoning_policy = ReasoningPreference.CLIENT
    settings.reasoning_fable = ReasoningPreference.INHERIT
    settings.reasoning_opus = ReasoningPreference.INHERIT
    settings.reasoning_sonnet = ReasoningPreference.INHERIT
    settings.reasoning_haiku = ReasoningPreference.INHERIT
    return settings


def _request(*, image: bool = False, model: str = "claude-opus-4") -> MessagesRequest:
    content: list[dict[str, object]] = [{"type": "text", "text": "describe this"}]
    if image:
        content.append(_IMAGE_BLOCK)
    return MessagesRequest.model_validate(
        {"model": model, "messages": [{"role": "user", "content": content}]}
    )


def _refs(router: ModelRouter, request: MessagesRequest) -> tuple[str, ...]:
    return router.resolve_messages_plan(request).model_refs()


def test_a_route_without_a_chain_yields_a_single_attempt(settings):
    assert _refs(ModelRouter(settings), _request()) == ("nvidia_nim/fallback-model",)


def test_a_route_without_an_override_uses_the_root_chain(settings):
    settings.model_fallbacks = "cerebras/one,groq/two"

    assert _refs(ModelRouter(settings), _request()) == (
        "nvidia_nim/fallback-model",
        "cerebras/one",
        "groq/two",
    )


def test_a_route_with_an_override_uses_its_own_chain_only(settings):
    settings.model_fallbacks = "cerebras/root-fallback"
    settings.model_opus = "open_router/opus-primary"
    settings.model_opus_fallbacks = "groq/opus-second"

    assert _refs(ModelRouter(settings), _request(model="claude-opus-4")) == (
        "open_router/opus-primary",
        "groq/opus-second",
    )
    # A route with no override of its own still falls back to the root chain.
    assert _refs(ModelRouter(settings), _request(model="claude-sonnet-4")) == (
        "nvidia_nim/fallback-model",
        "cerebras/root-fallback",
    )


def test_an_explicit_provider_model_request_is_never_overridden(settings):
    settings.model_fallbacks = "cerebras/one"

    assert _refs(ModelRouter(settings), _request(model="groq/exact/model")) == (
        "groq/exact/model",
    )


def test_a_chain_entry_naming_an_unknown_provider_is_skipped(settings):
    # Settings validation rejects these, but a custom provider can be removed
    # from the registry after the chain was persisted.
    settings.model_fallbacks = "not_a_provider/x,cerebras/real"

    assert _refs(ModelRouter(settings), _request()) == (
        "nvidia_nim/fallback-model",
        "cerebras/real",
    )


def test_a_duplicate_of_the_primary_is_dropped_from_the_chain(settings):
    settings.model_fallbacks = "nvidia_nim/fallback-model,cerebras/real"

    assert _refs(ModelRouter(settings), _request()) == (
        "nvidia_nim/fallback-model",
        "cerebras/real",
    )


def test_every_attempt_keeps_the_route_reasoning_preference(settings):
    settings.model_opus = "open_router/primary"
    settings.model_opus_fallbacks = "groq/second"
    settings.reasoning_opus = ReasoningPreference.OFF

    plan = ModelRouter(settings).resolve_messages_plan(_request(model="claude-opus-4"))

    assert [attempt.resolved.reasoning_preference for attempt in plan.attempts] == [
        ReasoningPreference.OFF,
        ReasoningPreference.OFF,
    ]


# ------------------------------------------------------------------- vision


def _blind(*blind_models: str):
    def lookup(provider_id: str, model_id: str) -> bool | None:
        if f"{provider_id}/{model_id}" in blind_models:
            return False
        return None

    return lookup


def test_an_image_reroutes_to_the_vision_adapter_when_the_route_is_blind(settings):
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    assert _refs(router, _request(image=True)) == ("open_router/sees-images",)


def test_a_text_only_request_never_reaches_the_vision_adapter(settings):
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    assert _refs(router, _request(image=False)) == ("nvidia_nim/fallback-model",)


def test_unknown_vision_capability_leaves_the_route_alone(settings):
    """Most providers publish no modality metadata; silence is not a refusal."""
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind())

    assert _refs(router, _request(image=True)) == ("nvidia_nim/fallback-model",)


def test_no_vision_adapter_configured_leaves_the_route_alone(settings):
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    assert _refs(router, _request(image=True)) == ("nvidia_nim/fallback-model",)


def test_blind_fallbacks_are_dropped_from_a_diverted_chain(settings):
    settings.model_fallbacks = "groq/also-blind,cerebras/unknown"
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(
        settings, vision_lookup=_blind("nvidia_nim/fallback-model", "groq/also-blind")
    )

    assert _refs(router, _request(image=True)) == (
        "open_router/sees-images",
        "cerebras/unknown",
    )


def _sight(capability: dict[str, bool]):
    """Vision lookup with explicit per-ref answers; unlisted refs stay unknown."""

    def lookup(provider_id: str, model_id: str) -> bool | None:
        return capability.get(f"{provider_id}/{model_id}")

    return lookup


def test_a_sighted_fallback_leads_when_no_vision_adapter_is_configured(settings):
    """A chain member that can see beats a model documented to reject images.

    Without this the image went to the known-blind primary, which either fails
    or -- worse -- answers about an image it never received.
    """
    settings.model_fallbacks = "groq/also-blind,cerebras/sees-images"
    router = ModelRouter(
        settings,
        vision_lookup=_sight(
            {
                "nvidia_nim/fallback-model": False,
                "groq/also-blind": False,
                "cerebras/sees-images": True,
            }
        ),
    )

    assert _refs(router, _request(image=True)) == ("cerebras/sees-images",)


def test_a_text_request_keeps_the_blind_models_in_the_chain(settings):
    settings.model_fallbacks = "groq/also-blind,cerebras/sees-images"
    router = ModelRouter(
        settings,
        vision_lookup=_sight(
            {
                "nvidia_nim/fallback-model": False,
                "groq/also-blind": False,
                "cerebras/sees-images": True,
            }
        ),
    )

    assert _refs(router, _request(image=False)) == (
        "nvidia_nim/fallback-model",
        "groq/also-blind",
        "cerebras/sees-images",
    )


def test_an_image_keeps_the_whole_route_when_nothing_is_known_to_see(settings):
    """Every candidate is blind: leave the route intact rather than route nowhere.

    Dropping the blind entries here would leave an empty plan, which is worse
    than letting the request fail against the model the user actually chose.
    """
    settings.model_fallbacks = "groq/also-blind"
    router = ModelRouter(
        settings,
        vision_lookup=_sight(
            {"nvidia_nim/fallback-model": False, "groq/also-blind": False}
        ),
    )

    assert _refs(router, _request(image=True)) == (
        "nvidia_nim/fallback-model",
        "groq/also-blind",
    )


def test_a_plan_records_the_vision_diversion_it_made(settings):
    """Without this the log cannot tell a diversion from an ordinary route."""
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    plan = router.resolve_messages_plan(_request(image=True))

    assert plan.diversion is RouteDiversion.VISION
    assert plan.diverted_from == "nvidia_nim/fallback-model"
    assert plan.model_refs() == ("open_router/sees-images",)


def test_an_undiverted_plan_records_no_diversion(settings):
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    plan = router.resolve_messages_plan(_request(image=False))

    assert plan.diversion is None
    assert plan.diverted_from is None


def test_a_sighted_chain_promotion_is_recorded_as_a_diversion(settings):
    """Promoting a sighted fallback also replaces the head of the chain."""
    settings.model_fallbacks = "cerebras/sees-images"
    router = ModelRouter(
        settings,
        vision_lookup=_sight(
            {"nvidia_nim/fallback-model": False, "cerebras/sees-images": True}
        ),
    )

    plan = router.resolve_messages_plan(_request(image=True))

    assert plan.diversion is RouteDiversion.VISION
    assert plan.diverted_from == "nvidia_nim/fallback-model"
