"""Application-owned model metadata."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderModelInfo:
    """Provider model metadata used to shape the application model catalog."""

    model_id: str
    supports_thinking: bool | None = None
    # ``None`` means the provider does not report image support, which is not
    # the same as reporting that it has none: vision routing only diverts a
    # request when a model is known to lack it.
    supports_vision: bool | None = None
    context_length: int | None = None
    input_price: float | None = None
    output_price: float | None = None


@dataclass(frozen=True, slots=True)
class ProviderModelRefreshResult:
    """Per-provider outcome of one model-catalog refresh."""

    refreshed_provider_ids: tuple[str, ...] = ()
    failed_provider_ids: tuple[str, ...] = ()
