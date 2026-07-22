"""SearchAPI.io adapter (GET www.searchapi.io/api/v1/search, api_key query param).

Result URLs live in the ``link`` field; error payloads can arrive with HTTP 200,
so the body is checked for a top-level ``error``.
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import SEARCHAPI_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..errors import WebSearchUpstreamError
from .http import build_async_client, request_json


class SearchApiWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "searchapi"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or SEARCHAPI_DEFAULT_BASE).rstrip("/")
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
            f"{self._base_url}/api/v1/search",
            params={
                "engine": "google",
                "q": query,
                "api_key": key,
                "num": max_results,
            },
        )
        if isinstance(data, dict) and data.get("error"):
            raise WebSearchUpstreamError(
                self.provider_id, f"search error: {data['error']}"
            )
        rows = data.get("organic_results", []) if isinstance(data, dict) else []
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _text(row.get("link"))
            if not url:
                continue
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("title")),
                    url=url,
                    snippet=_text(row.get("snippet")),
                    content=None,
                    published=_text(row.get("date")) or None,
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
