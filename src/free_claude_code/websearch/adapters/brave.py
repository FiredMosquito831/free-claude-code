"""Brave Search adapter (GET api.search.brave.com/res/v1/web/search)."""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import BRAVE_SEARCH_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from .http import build_async_client, request_json

_MAX_COUNT = 20


class BraveWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "brave"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or BRAVE_SEARCH_DEFAULT_BASE).rstrip("/")
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
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "GET",
            f"{self._base_url}/res/v1/web/search",
            headers={"X-Subscription-Token": key},
            params={"q": query, "count": min(max_results, _MAX_COUNT)},
        )
        web = data.get("web", {}) if isinstance(data, dict) else {}
        rows = web.get("results", []) if isinstance(web, dict) else []
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
                    snippet=_text(row.get("description")),
                    content=None,
                    published=_text(row.get("page_age")) or None,
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
