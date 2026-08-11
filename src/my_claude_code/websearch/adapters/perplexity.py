"""Perplexity Search adapter (POST api.perplexity.ai/search with Bearer auth).

Stale keys minted before the Search API cutoff fail with HTTP 451, which maps
to an auth error. ``search_domain_filter`` takes allows OR ``-`` denies, never
mixed.
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import PERPLEXITY_SEARCH_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..errors import WebSearchAuthError, WebSearchError
from ..options import option_int
from .http import build_async_client, request_json

_EXTRA_STATUS_ERRORS: dict[int, type[WebSearchError]] = {451: WebSearchAuthError}


class PerplexityWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "perplexity"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or PERPLEXITY_SEARCH_DEFAULT_BASE).rstrip("/")
        self._client = build_async_client(
            proxy=config.proxy, http_timeout=config.http_timeout
        )

    async def _search_with_key(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int,
        allowed_domains: tuple[str, ...],
        blocked_domains: tuple[str, ...],
    ) -> WebSearchResponse:
        options = self._config.options
        payload: dict[str, Any] = {"query": query, "max_results": max_results}
        if recency := options.get("PERPLEXITY_SEARCH_RECENCY", ""):
            payload["search_recency_filter"] = recency
        # Upstream: use max_tokens_per_page OR search_context_size, not both.
        if (
            max_tokens := option_int(options.get("PERPLEXITY_MAX_TOKENS_PER_PAGE"))
        ) is not None:
            payload["max_tokens_per_page"] = max_tokens
        elif context_size := options.get("PERPLEXITY_CONTEXT_SIZE", ""):
            payload["search_context_size"] = context_size
        if allowed_domains:
            payload["search_domain_filter"] = list(allowed_domains)
        elif blocked_domains:
            payload["search_domain_filter"] = [
                f"-{domain}" for domain in blocked_domains
            ]
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/search",
            headers={"Authorization": f"Bearer {key}"},
            json_body=payload,
            extra_status_errors=_EXTRA_STATUS_ERRORS,
        )
        rows = data.get("results", []) if isinstance(data, dict) else []
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _text(row.get("url"))
            if not url:
                continue
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("title")),
                    url=url,
                    snippet=_text(row.get("snippet")),
                    content=None,
                    published=(_text(row.get("date")) or _text(row.get("last_updated")))
                    or None,
                )
            )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
        )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
