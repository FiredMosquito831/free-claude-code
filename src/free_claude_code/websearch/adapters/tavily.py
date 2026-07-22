"""Tavily adapter (POST api.tavily.com/search with Bearer auth).

``search_depth`` is pinned to ``basic`` so ``auto_parameters`` cannot silently
upgrade cost; HTTP 432 (plan usage limit) maps to a quota error.
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import TAVILY_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..errors import WebSearchError, WebSearchQuotaError
from .http import build_async_client, request_json

_EXTRA_STATUS_ERRORS: dict[int, type[WebSearchError]] = {432: WebSearchQuotaError}


class TavilyWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "tavily"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or TAVILY_DEFAULT_BASE).rstrip("/")
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
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        if allowed_domains:
            payload["include_domains"] = list(allowed_domains)
        if blocked_domains:
            payload["exclude_domains"] = list(blocked_domains)
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
                    snippet=_text(row.get("content")),
                    content=_text(row.get("raw_content")) or None,
                    published=None,
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
