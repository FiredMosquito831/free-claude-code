"""Firecrawl adapter (POST api.firecrawl.dev/v2/search with Bearer auth)."""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import FIRECRAWL_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from .http import build_async_client, request_json


class FirecrawlWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "firecrawl"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or FIRECRAWL_DEFAULT_BASE).rstrip("/")
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
            "limit": max_results,
            "sources": ["web"],
        }
        # includeDomains/excludeDomains are mutually exclusive upstream.
        if allowed_domains:
            payload["includeDomains"] = list(allowed_domains)
        elif blocked_domains:
            payload["excludeDomains"] = list(blocked_domains)
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/v2/search",
            headers={"Authorization": f"Bearer {key}"},
            json_body=payload,
        )
        section = data.get("data", {}) if isinstance(data, dict) else {}
        rows = section.get("web", []) if isinstance(section, dict) else []
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
                    content=_text(row.get("markdown")) or None,
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
