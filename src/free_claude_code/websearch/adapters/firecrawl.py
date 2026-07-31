"""Firecrawl adapter (POST api.firecrawl.dev/v2/search with Bearer auth).

Advanced dotenv options: ``FIRECRAWL_SOURCES`` (web/news/images arrays),
``FIRECRAWL_SCRAPE_FORMAT`` per-result scrape (summary -> snippet upgrade,
markdown -> item.content; multiplies credits), ``FIRECRAWL_TBS`` date filter
and ``FIRECRAWL_LOCATION`` geo.
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import FIRECRAWL_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from .http import build_async_client, request_json

_KNOWN_SOURCES = ("web", "news", "images")


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
        options = self._config.options
        sources = self._sources()
        payload: dict[str, Any] = {
            "query": query,
            # ``limit`` is per source type upstream, so asking for
            # ``max_results`` from each of N sources fetches (and bills)
            # N times what the caller can use.
            "limit": max(1, -(-max_results // max(1, len(sources)))),
            "sources": sources,
        }
        if country := options.get("FIRECRAWL_COUNTRY", ""):
            payload["country"] = country
        if categories := options.get("FIRECRAWL_CATEGORIES", ""):
            payload["categories"] = [
                entry.strip() for entry in categories.split(",") if entry.strip()
            ]
        if scrape_format := options.get("FIRECRAWL_SCRAPE_FORMAT", ""):
            payload["scrapeOptions"] = {"formats": [{"type": scrape_format}]}
        if tbs := options.get("FIRECRAWL_TBS", ""):
            payload["tbs"] = tbs
        if location := options.get("FIRECRAWL_LOCATION", ""):
            payload["location"] = location
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
        items = []
        for source in sources:
            if source not in _KNOWN_SOURCES:
                continue
            rows = section.get(source, []) if isinstance(section, dict) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = self._map_row(source, row)
                if item is not None:
                    items.append(item)
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
        )

    def _sources(self) -> list[str]:
        raw = self._config.options.get("FIRECRAWL_SOURCES", "")
        sources = [part.strip() for part in raw.split(",") if part.strip()]
        return sources or ["web"]

    @staticmethod
    def _map_row(source: str, row: dict[str, Any]) -> WebSearchResultItem | None:
        if source == "images":
            url = _text(row.get("imageUrl")) or _text(row.get("url"))
            if not url:
                return None
            return WebSearchResultItem(
                title=_text(row.get("title")),
                url=url,
                snippet="",
                content=None,
                published=None,
            )
        url = _text(row.get("url"))
        if not url:
            return None
        if source == "news":
            return WebSearchResultItem(
                title=_text(row.get("title")),
                url=url,
                snippet=_text(row.get("snippet")) or _text(row.get("description")),
                content=_text(row.get("markdown")) or None,
                published=_text(row.get("date")) or None,
            )
        return WebSearchResultItem(
            title=_text(row.get("title")),
            url=url,
            snippet=_text(row.get("summary")) or _text(row.get("description")),
            content=_text(row.get("markdown")) or None,
            published=None,
        )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
