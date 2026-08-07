"""Provider model-list metadata cache."""

from collections.abc import Iterable

from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.config.provider_registry import get_provider_registry
from free_claude_code.providers.model_listing import model_infos_from_ids


class ProviderModelCache:
    """Store provider model metadata for instant model-list responses."""

    def __init__(
        self,
        available_provider_ids: Iterable[str] | None = None,
    ) -> None:
        if available_provider_ids is None:
            available_provider_ids = get_provider_registry().supported_ids()
        self._available_provider_ids = frozenset(available_provider_ids)
        self._model_infos_by_provider: dict[str, dict[str, ProviderModelInfo]] = {}

    def cache_model_ids(self, provider_id: str, model_ids: Iterable[str]) -> None:
        """Store raw provider model ids with unknown capability metadata."""
        self.cache_model_infos(provider_id, model_infos_from_ids(model_ids))

    def cache_model_infos(
        self, provider_id: str, model_infos: Iterable[ProviderModelInfo]
    ) -> None:
        """Store provider model metadata by raw provider model id."""
        if provider_id not in self._available_provider_ids:
            return
        clean_infos = {
            info.model_id: info for info in model_infos if info.model_id.strip()
        }
        self._model_infos_by_provider[provider_id] = clean_infos

    def set_available_providers(self, provider_ids: Iterable[str]) -> None:
        """Replace the provider scope and discard entries outside it."""
        self._available_provider_ids = frozenset(provider_ids)
        self._model_infos_by_provider = {
            provider_id: infos
            for provider_id, infos in self._model_infos_by_provider.items()
            if provider_id in self._available_provider_ids
        }

    def cached_model_ids(self) -> dict[str, frozenset[str]]:
        """Return cached raw provider model ids by provider."""
        return {
            provider_id: frozenset(infos)
            for provider_id, infos in self._model_infos_by_provider.items()
        }

    def has_provider(self, provider_id: str) -> bool:
        """Return whether this provider has any cached model-list result."""
        return provider_id in self._model_infos_by_provider

    def cached_model_supports_thinking(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        """Return cached thinking support when a provider exposes it."""
        info = self._model_infos_by_provider.get(provider_id, {}).get(model_id)
        if info is None:
            return None
        return info.supports_thinking

    def cached_model_supports_vision(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        """Return cached image-input support when a provider exposes it."""
        info = self._model_infos_by_provider.get(provider_id, {}).get(model_id)
        if info is None:
            return None
        return info.supports_vision

    def cached_prefixed_model_refs(self) -> tuple[str, ...]:
        """Return cached provider models in user-selectable ``provider/model`` form."""
        return tuple(info.model_id for info in self.cached_prefixed_model_infos())

    def cached_prefixed_model_infos(self) -> tuple[ProviderModelInfo, ...]:
        """Return cached provider models with user-selectable prefixed ids."""
        infos: list[ProviderModelInfo] = []
        supported_ids = get_provider_registry().supported_ids()
        ordered_ids = [
            provider_id
            for provider_id in supported_ids
            if provider_id in self._available_provider_ids
        ]
        ordered_ids.extend(
            provider_id
            for provider_id in self._model_infos_by_provider
            if provider_id not in supported_ids
        )
        for provider_id in ordered_ids:
            provider_infos = self._model_infos_by_provider.get(provider_id, {})
            infos.extend(
                ProviderModelInfo(
                    model_id=f"{provider_id}/{info.model_id}",
                    supports_thinking=info.supports_thinking,
                    supports_vision=info.supports_vision,
                    context_length=info.context_length,
                    input_price=info.input_price,
                    output_price=info.output_price,
                )
                for info in sorted(
                    provider_infos.values(), key=lambda item: item.model_id
                )
            )
        return tuple(infos)

    def clear(self) -> None:
        """Clear all cached model metadata."""
        self._model_infos_by_provider.clear()
