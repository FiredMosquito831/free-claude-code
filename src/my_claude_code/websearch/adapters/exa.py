"""Exa adapter (POST api.exa.ai/search with x-api-key auth).

Snippets are requested via the ``contents.highlights`` opt-in; the provider's
``costDollars.total`` is surfaced as ``cost_usd``. Advanced dotenv options:
``EXA_SEARCH_TYPE`` (deep* costs more), ``EXA_CONTENTS`` content modes,
``EXA_CATEGORY`` verticals (company/people skip date+exclude filters),
date/geo filters, and ``EXA_MAX_AGE_HOURS`` crawl freshness.
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import EXA_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..options import option_int
from .http import build_async_client, request_json

_SNIPPET_CHARS = 1000
# company/people categories reject date filters and excludeDomains upstream.
_CATEGORIES_WITHOUT_DATE_OR_EXCLUDE = frozenset({"company", "people"})

_CONTENTS_MODES: dict[str, dict[str, bool]] = {
    "highlights": {"highlights": True},
    "text": {"text": True},
    "highlights+text": {"highlights": True, "text": True},
    "highlights+summary": {"highlights": True, "summary": True},
    "full": {"highlights": True, "text": True, "summary": True},
}


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
        options = self._config.options
        category = options.get("EXA_CATEGORY", "")
        category_restricted = category in _CATEGORIES_WITHOUT_DATE_OR_EXCLUDE
        payload: dict[str, Any] = {
            "query": query,
            "numResults": max_results,
            "contents": self._contents_payload(),
        }
        if search_type := options.get("EXA_SEARCH_TYPE", ""):
            payload["type"] = search_type
        if category:
            payload["category"] = category
        if not category_restricted:
            if start := options.get("EXA_START_PUBLISHED_DATE", ""):
                payload["startPublishedDate"] = start
            if end := options.get("EXA_END_PUBLISHED_DATE", ""):
                payload["endPublishedDate"] = end
        if location := options.get("EXA_USER_LOCATION", ""):
            payload["userLocation"] = location
        if allowed_domains:
            payload["includeDomains"] = list(allowed_domains)
        if blocked_domains and not category_restricted:
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
            summary = _text(row.get("summary"))
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("title")),
                    url=url,
                    snippet=(snippet or summary or text)[:_SNIPPET_CHARS],
                    content=(text or summary) or None,
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

    def _contents_payload(self) -> dict[str, Any]:
        options = self._config.options
        mode = options.get("EXA_CONTENTS", "")
        contents: dict[str, Any] = dict(_CONTENTS_MODES.get(mode, {"highlights": True}))
        if (max_age := option_int(options.get("EXA_MAX_AGE_HOURS"))) is not None:
            contents["maxAgeHours"] = max_age
        return contents


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
