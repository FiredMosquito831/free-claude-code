"""SearXNG adapter (keyless, self-hosted; GET {base}/search?format=json)."""

from typing import Any, ClassVar

from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..errors import WebSearchAuthError, WebSearchConfigError
from .http import build_async_client, request_json


class SearxngWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "searxng"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        if not config.base_url or not config.base_url.strip():
            raise WebSearchConfigError(
                self.provider_id,
                "SEARXNG_BASE_URL is required for the searxng provider "
                "(point it at a self-hosted instance with format=json enabled)",
            )
        self._base_url = config.base_url.rstrip("/")
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
        params: dict[str, Any] = {"q": query, "format": "json"}
        if engines := options.get("SEARXNG_ENGINES", ""):
            params["engines"] = engines
        if categories := options.get("SEARXNG_CATEGORIES", ""):
            params["categories"] = categories
        if time_range := options.get("SEARXNG_TIME_RANGE", ""):
            params["time_range"] = time_range
        if language := options.get("SEARXNG_LANGUAGE", ""):
            params["language"] = language
        try:
            data = await request_json(
                self._require_client(),
                self.provider_id,
                "GET",
                f"{self._base_url}/search",
                params=params,
            )
        except WebSearchAuthError as error:
            # Most public instances disable format=json and answer 403.
            raise WebSearchAuthError(
                self.provider_id,
                "SearXNG instance rejected the request; enable format=json in "
                f"its settings.yml ({error.message})",
                status_code=error.status_code,
            ) from error
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
                    content=None,
                    published=_text(row.get("publishedDate")) or None,
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
