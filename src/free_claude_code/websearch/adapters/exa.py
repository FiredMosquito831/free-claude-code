"""Exa adapter (POST api.exa.ai/search with x-api-key auth).

Snippets are requested via the ``contents.highlights`` opt-in; the provider's
``costDollars.total`` is surfaced as ``cost_usd``.
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import EXA_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from .http import build_async_client, request_json

_SNIPPET_CHARS = 1000


class ExaWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "exa"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or EXA_DEFAULT_BASE).rstrip("/")
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
            "numResults": max_results,
            "contents": {"highlights": True},
        }
        if allowed_domains:
            payload["includeDomains"] = list(allowed_domains)
        if blocked_domains:
            payload["excludeDomains"] = list(blocked_domains)
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/search",
            headers={"x-api-key": key},
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
            highlights = row.get("highlights")
            snippet = (
                " … ".join(part for part in highlights if isinstance(part, str))
                if isinstance(highlights, list)
                else ""
            )
            text = _text(row.get("text"))
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("title")),
                    url=url,
                    snippet=(snippet or text)[:_SNIPPET_CHARS],
                    content=text or None,
                    published=_text(row.get("publishedDate")) or None,
                )
            )
        cost = (
            data.get("costDollars", {}).get("total") if isinstance(data, dict) else None
        )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
        )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
