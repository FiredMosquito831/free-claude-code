"""Linkup adapter (POST api.linkup.so/v1/search with Bearer auth).

Result titles live in the ``name`` field (not ``title``).
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import LINKUP_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from .http import build_async_client, request_json


class LinkupWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "linkup"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or LINKUP_DEFAULT_BASE).rstrip("/")
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
            "q": query,
            "depth": "standard",
            "outputType": "searchResults",
        }
        if allowed_domains:
            payload["includeDomains"] = list(allowed_domains)
        if blocked_domains:
            payload["excludeDomains"] = list(blocked_domains)
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/v1/search",
            headers={"Authorization": f"Bearer {key}"},
            json_body=payload,
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
                    title=_text(row.get("name")),
                    url=url,
                    snippet=_text(row.get("content")),
                    content=None,
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
