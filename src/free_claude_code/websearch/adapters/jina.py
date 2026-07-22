"""Jina Search adapter (GET s.jina.ai/{url-encoded query}, JSON mode).

``content`` is the full Reader extraction of each hit (long); snippets are
truncated to ~1000 chars while the full text stays in ``content``.
"""

from typing import Any, ClassVar
from urllib.parse import quote

from free_claude_code.config.websearch_catalog import JINA_SEARCH_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from .http import build_async_client, request_json

_SNIPPET_CHARS = 1000


class JinaWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "jina"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or JINA_SEARCH_DEFAULT_BASE).rstrip("/")
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
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
        if max_tokens := options.get("JINA_MAX_TOKENS", ""):
            headers["X-Max-Tokens"] = max_tokens
        params: dict[str, Any] = {}
        if site := options.get("JINA_SITE", ""):
            params["site"] = site
        if gl := options.get("JINA_GL", ""):
            params["gl"] = gl
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "GET",
            f"{self._base_url}/{quote(query, safe='')}",
            headers=headers,
            params=params or None,
        )
        rows = data.get("data", []) if isinstance(data, dict) else []
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _text(row.get("url"))
            if not url:
                continue
            content = _text(row.get("content"))
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("title")),
                    url=url,
                    snippet=content[:_SNIPPET_CHARS],
                    content=content or None,
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
