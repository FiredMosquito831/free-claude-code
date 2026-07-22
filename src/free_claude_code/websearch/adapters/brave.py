"""Brave Search adapter (GET api.search.brave.com/res/v1/web/search).

``BRAVE_SEARCH_MODE=llm-context`` switches to POST /res/v1/llm/context, which
returns pre-extracted page text under ``grounding.generic[]`` instead of the
classic web SERP shape. Web mode extras: extra_snippets/freshness/country/
search_lang query params.
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import BRAVE_SEARCH_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..options import option_enabled, option_int
from .http import build_async_client, request_json

_MAX_COUNT = 20
_MAX_LLM_URLS = 50
_SNIPPET_CHARS = 1000


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
        if self._config.options.get("BRAVE_SEARCH_MODE", "") == "llm-context":
            return await self._search_llm_context(query, key, key_index, max_results)
        return await self._search_web(query, key, key_index, max_results)

    async def _search_web(
        self, query: str, key: str, key_index: int, max_results: int
    ) -> WebSearchResponse:
        options = self._config.options
        params: dict[str, Any] = {"q": query, "count": min(max_results, _MAX_COUNT)}
        if option_enabled(options.get("BRAVE_EXTRA_SNIPPETS")):
            params["extra_snippets"] = "true"
        if freshness := options.get("BRAVE_FRESHNESS", ""):
            params["freshness"] = freshness
        if country := options.get("BRAVE_COUNTRY", ""):
            params["country"] = country
        if search_lang := options.get("BRAVE_SEARCH_LANG", ""):
            params["search_lang"] = search_lang
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "GET",
            f"{self._base_url}/res/v1/web/search",
            headers={"X-Subscription-Token": key},
            params=params,
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
            extra = row.get("extra_snippets")
            extra_text = (
                "\n\n".join(part for part in extra if isinstance(part, str))
                if isinstance(extra, list)
                else ""
            )
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("title")),
                    url=url,
                    snippet=_text(row.get("description")),
                    content=extra_text or None,
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

    async def _search_llm_context(
        self, query: str, key: str, key_index: int, max_results: int
    ) -> WebSearchResponse:
        options = self._config.options
        payload: dict[str, Any] = {
            "q": query,
            "maximum_number_of_urls": min(max_results, _MAX_LLM_URLS),
        }
        if (max_tokens := option_int(options.get("BRAVE_LLM_MAX_TOKENS"))) is not None:
            payload["maximum_number_of_tokens"] = max_tokens
        if freshness := options.get("BRAVE_FRESHNESS", ""):
            payload["freshness"] = freshness
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/res/v1/llm/context",
            headers={"X-Subscription-Token": key},
            json_body=payload,
        )
        grounding = data.get("grounding", {}) if isinstance(data, dict) else {}
        rows = grounding.get("generic", []) if isinstance(grounding, dict) else []
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _text(row.get("url"))
            if not url:
                continue
            snippets = row.get("snippets")
            text = (
                "\n\n".join(part for part in snippets if isinstance(part, str))
                if isinstance(snippets, list)
                else ""
            )
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("title")),
                    url=url,
                    snippet=text[:_SNIPPET_CHARS],
                    content=text or None,
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
