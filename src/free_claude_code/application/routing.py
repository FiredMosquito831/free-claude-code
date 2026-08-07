"""Model routing for Claude-compatible requests."""

from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

from free_claude_code.application.errors import UnknownProviderError
from free_claude_code.config.model_refs import (
    parse_model_name,
    parse_model_ref_list,
    parse_provider_type,
)
from free_claude_code.config.provider_registry import get_provider_registry
from free_claude_code.config.reasoning import ReasoningPreference
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic import (
    MessagesRequest,
    TokenCountRequest,
    request_carries_image,
)
from free_claude_code.core.gateway_model_ids import decode_gateway_model_id
from free_claude_code.core.reasoning import ReasoningPolicy

from .reasoning import resolve_reasoning_policy

_ROUTE_SETTINGS = (
    ("fable", "model_fable", "reasoning_fable", "model_fable_fallbacks"),
    ("opus", "model_opus", "reasoning_opus", "model_opus_fallbacks"),
    ("haiku", "model_haiku", "reasoning_haiku", "model_haiku_fallbacks"),
    ("sonnet", "model_sonnet", "reasoning_sonnet", "model_sonnet_fallbacks"),
)


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    original_model: str
    provider_id: str
    provider_model: str
    provider_model_ref: str
    reasoning_preference: ReasoningPreference


@dataclass(frozen=True, slots=True)
class RoutedMessagesRequest:
    request: MessagesRequest
    resolved: ResolvedModel
    reasoning: ReasoningPolicy


@dataclass(frozen=True, slots=True)
class RoutedTokenCountRequest:
    request: TokenCountRequest
    resolved: ResolvedModel


@dataclass(frozen=True, slots=True)
class RoutedMessagesPlan:
    """One request and the ordered alternates to try if it cannot be served.

    ``attempts[0]`` is what the route resolves to today; everything after it is
    a configured fallback. A plan with a single attempt behaves exactly like the
    unchained routing it replaces.
    """

    attempts: tuple[RoutedMessagesRequest, ...]

    def __post_init__(self) -> None:
        if not self.attempts:
            raise ValueError("A routed messages plan needs at least one attempt.")

    @property
    def primary(self) -> RoutedMessagesRequest:
        return self.attempts[0]

    @property
    def has_fallbacks(self) -> bool:
        return len(self.attempts) > 1

    def model_refs(self) -> tuple[str, ...]:
        return tuple(attempt.resolved.provider_model_ref for attempt in self.attempts)


VisionCapabilityLookup = Callable[[str, str], bool | None]


class ModelRouter:
    """Resolve incoming Claude model names to configured provider/model pairs."""

    def __init__(
        self,
        settings: Settings,
        *,
        vision_lookup: VisionCapabilityLookup | None = None,
    ):
        self._settings = settings
        self._vision_lookup = vision_lookup

    def resolve(self, claude_model_name: str) -> ResolvedModel:
        (
            direct_provider_id,
            direct_provider_model,
            force_reasoning_off,
        ) = self._direct_provider_model(claude_model_name)
        if direct_provider_id is not None and direct_provider_model is not None:
            reasoning_preference = (
                ReasoningPreference.OFF
                if force_reasoning_off
                else self._settings.reasoning_policy
            )
            logger.debug(
                "MODEL DIRECT: '{}' -> provider='{}' model='{}' reasoning={}",
                claude_model_name,
                direct_provider_id,
                direct_provider_model,
                reasoning_preference.value,
            )
            return ResolvedModel(
                original_model=claude_model_name,
                provider_id=direct_provider_id,
                provider_model=direct_provider_model,
                provider_model_ref=claude_model_name,
                reasoning_preference=reasoning_preference,
            )

        provider_model_ref = self._resolve_model_ref(claude_model_name)
        reasoning_preference = self._resolve_reasoning_preference(claude_model_name)
        provider_id = parse_provider_type(provider_model_ref)
        self._validate_provider_id(provider_id)
        provider_model = parse_model_name(provider_model_ref)
        if provider_model != claude_model_name:
            logger.debug(
                "MODEL MAPPING: '{}' -> '{}'", claude_model_name, provider_model
            )
        return ResolvedModel(
            original_model=claude_model_name,
            provider_id=provider_id,
            provider_model=provider_model,
            provider_model_ref=provider_model_ref,
            reasoning_preference=reasoning_preference,
        )

    @staticmethod
    def _validate_provider_id(provider_id: str) -> None:
        descriptors = get_provider_registry().all_descriptors()
        if provider_id not in descriptors:
            raise UnknownProviderError.for_provider(provider_id, descriptors)

    def _direct_provider_model(
        self, model_name: str
    ) -> tuple[str | None, str | None, bool]:
        supported_ids = get_provider_registry().supported_ids()
        decoded = decode_gateway_model_id(model_name)
        if decoded is not None:
            if decoded.provider_id not in supported_ids:
                return None, None, False
            return (
                decoded.provider_id,
                decoded.provider_model,
                decoded.force_reasoning_off,
            )

        provider_id, separator, provider_model = model_name.partition("/")
        if not separator:
            return None, None, False
        if provider_id not in supported_ids:
            return None, None, False
        if not provider_model:
            return None, None, False
        return provider_id, provider_model, False

    def resolve_chain(self, claude_model_name: str) -> tuple[ResolvedModel, ...]:
        """Resolve a Claude model name to its ordered primary/fallback chain.

        A client that names a provider and model directly gets exactly what it
        asked for: overriding an explicit choice with a configured fallback
        would silently answer a different question than the one asked.
        """

        primary = self.resolve(claude_model_name)
        direct_provider_id, _model, _off = self._direct_provider_model(
            claude_model_name
        )
        if direct_provider_id is not None:
            return (primary,)

        reasoning_preference = self._resolve_reasoning_preference(claude_model_name)
        resolved = [primary]
        seen = {primary.provider_model_ref}
        for model_ref in self._fallback_model_refs(claude_model_name):
            if model_ref in seen:
                continue
            seen.add(model_ref)
            provider_id = parse_provider_type(model_ref)
            try:
                self._validate_provider_id(provider_id)
            except UnknownProviderError:
                # A chain is a resilience feature: one unusable entry must not
                # take down a route whose primary is perfectly healthy.
                logger.warning(
                    "MODEL FALLBACK SKIPPED: '{}' names unknown provider '{}'",
                    model_ref,
                    provider_id,
                )
                continue
            resolved.append(
                ResolvedModel(
                    original_model=claude_model_name,
                    provider_id=provider_id,
                    provider_model=parse_model_name(model_ref),
                    provider_model_ref=model_ref,
                    reasoning_preference=reasoning_preference,
                )
            )
        return tuple(resolved)

    def _fallback_model_refs(self, claude_model_name: str) -> tuple[str, ...]:
        """Return the fallback chain that sits next to this route's primary."""

        route = self._matched_route(claude_model_name)
        if route is not None and isinstance(getattr(self._settings, route[1]), str):
            return parse_model_ref_list(getattr(self._settings, route[3]))
        return parse_model_ref_list(self._settings.model_fallbacks)

    def _resolve_model_ref(self, claude_model_name: str) -> str:
        """Resolve a Claude model name to the configured provider/model ref."""

        route = self._matched_route(claude_model_name)
        if route is not None:
            model = getattr(self._settings, route[1])
            if isinstance(model, str):
                return model
        return self._settings.model

    def _resolve_reasoning_preference(
        self, claude_model_name: str
    ) -> ReasoningPreference:
        """Resolve a route override without inspecting the provider model."""

        route = self._matched_route(claude_model_name)
        if route is not None:
            preference = getattr(self._settings, route[2])
            if preference is not ReasoningPreference.INHERIT:
                return preference
        return self._settings.reasoning_policy

    @staticmethod
    def _matched_route(model_name: str) -> tuple[str, str, str, str] | None:
        normalized = model_name.lower()
        return next(
            (route for route in _ROUTE_SETTINGS if route[0] in normalized),
            None,
        )

    def resolve_messages_request(
        self, request: MessagesRequest
    ) -> RoutedMessagesRequest:
        """Return an internal routed request context."""
        return self._route_for(request, self.resolve(request.model))

    def resolve_messages_plan(self, request: MessagesRequest) -> RoutedMessagesPlan:
        """Return the primary routed request plus its configured fallbacks."""
        chain = self._apply_vision_policy(request, self.resolve_chain(request.model))
        plan = RoutedMessagesPlan(
            tuple(self._route_for(request, resolved) for resolved in chain)
        )
        if plan.has_fallbacks:
            logger.debug(
                "MODEL CHAIN: '{}' -> {}",
                request.model,
                " -> ".join(plan.model_refs()),
            )
        return plan

    def _apply_vision_policy(
        self, request: MessagesRequest, chain: tuple[ResolvedModel, ...]
    ) -> tuple[ResolvedModel, ...]:
        """Divert an image-carrying request to the vision adapter model.

        The diversion only happens when the route's model is *known* to reject
        images. An unknown capability is left alone: most providers publish no
        modality metadata at all, and rerouting on silence would move traffic
        away from models that handle images perfectly well.
        """
        vision_ref = self._settings.model_vision
        if not vision_ref or not request_carries_image(request):
            return chain
        if self._supports_vision(chain[0]) is not False:
            return chain

        vision_model = ResolvedModel(
            original_model=chain[0].original_model,
            provider_id=parse_provider_type(vision_ref),
            provider_model=parse_model_name(vision_ref),
            provider_model_ref=vision_ref,
            reasoning_preference=chain[0].reasoning_preference,
        )
        try:
            self._validate_provider_id(vision_model.provider_id)
        except UnknownProviderError:
            logger.warning(
                "VISION ROUTE SKIPPED: '{}' names unknown provider '{}'",
                vision_ref,
                vision_model.provider_id,
            )
            return chain

        # Fallbacks that are themselves known to be blind would answer a
        # question about an image they cannot see, which is worse than failing.
        sighted_fallbacks = tuple(
            resolved
            for resolved in chain
            if resolved.provider_model_ref != vision_ref
            and self._supports_vision(resolved) is not False
        )
        logger.info(
            "VISION ROUTE: '{}' carries an image and '{}' cannot read it; using '{}'",
            chain[0].original_model,
            chain[0].provider_model_ref,
            vision_ref,
        )
        return (vision_model, *sighted_fallbacks)

    def _supports_vision(self, resolved: ResolvedModel) -> bool | None:
        if self._vision_lookup is None:
            return None
        return self._vision_lookup(resolved.provider_id, resolved.provider_model)

    @staticmethod
    def _route_for(
        request: MessagesRequest, resolved: ResolvedModel
    ) -> RoutedMessagesRequest:
        routed = request.model_copy(deep=True)
        routed.model = resolved.provider_model
        return RoutedMessagesRequest(
            request=routed,
            resolved=resolved,
            reasoning=resolve_reasoning_policy(
                routed,
                resolved.reasoning_preference,
            ),
        )

    def resolve_token_count_request(
        self, request: TokenCountRequest
    ) -> RoutedTokenCountRequest:
        """Return an internal token-count request context."""
        resolved = self.resolve(request.model)
        routed = request.model_copy(
            update={"model": resolved.provider_model}, deep=True
        )
        return RoutedTokenCountRequest(request=routed, resolved=resolved)
