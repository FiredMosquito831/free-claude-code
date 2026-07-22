"""Parallel Search adapter (POST api.parallel.ai/v1beta/search, beta).

Snippets are the provider's token-compressed ``excerpts`` joined per result;
the beta opt-in header is required by the API.
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import PARALLEL_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..options import option_int
from .http import build_async_client, request_json

_SNIPPET_CHARS = 1000
_BETA_HEADER = "search-excerpt-2025-10-10"


class ParallelWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "parallel"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or PARALLEL_DEFAULT_BASE).rstrip("/")
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
        payload: dict[str, Any] = {
            "objective": query,
            "search_queries": [query],
            "max_results": max_results,
        }
        if mode := options.get("PARALLEL_MODE", ""):
            payload["mode"] = mode
        if (
            excerpt_chars := option_int(options.get("PARALLEL_EXCERPT_CHARS"))
        ) is not None:
            payload["excerpts"] = {"max_chars_per_result": excerpt_chars}
        if (total_chars := option_int(options.get("PARALLEL_TOTAL_CHARS"))) is not None:
            payload["max_chars_total"] = total_chars
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/v1beta/search",
            headers={"x-api-key": key, "parallel-beta": _BETA_HEADER},
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
            excerpts = row.get("excerpts")
            content = (
                "\n\n".join(part for part in excerpts if isinstance(part, str))
                if isinstance(excerpts, list)
                else ""
            )
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("title")),
                    url=url,
                    snippet=content[:_SNIPPET_CHARS],
                    content=content or None,
                    published=_text(row.get("publish_date")) or None,
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
