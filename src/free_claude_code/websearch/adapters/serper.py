"""Serper adapter (POST google.serper.dev/search with X-API-KEY auth).

Result URLs live in the ``link`` field (not ``url``).
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import SERPER_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from .http import build_async_client, request_json


class SerperWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "serper"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or SERPER_DEFAULT_BASE).rstrip("/")
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
            "POST",
            f"{self._base_url}/search",
            headers={"X-API-KEY": key},
            json_body={"q": query, "num": max_results},
        )
        rows = data.get("organic", []) if isinstance(data, dict) else []
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
