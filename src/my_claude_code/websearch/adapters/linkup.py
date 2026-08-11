"""Linkup adapter (POST api.linkup.so/v1/search with Bearer auth).

Result titles live in the ``name`` field (not ``title``). Advanced dotenv
options: ``LINKUP_DEPTH`` (deep = 10x cost) and ``LINKUP_OUTPUT_TYPE``;
``sourcedAnswer`` returns an LLM ``answer`` plus ``sources[]`` (mapped to
result items, answer -> response.answer).
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import LINKUP_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from .http import build_async_client, request_json

_SNIPPET_CHARS = 1000


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
        options = self._config.options
        output_type = options.get("LINKUP_OUTPUT_TYPE", "") or "searchResults"
        payload: dict[str, Any] = {
            "q": query,
            "depth": options.get("LINKUP_DEPTH", "") or "standard",
            "outputType": output_type,
            # Without this the API returns its own default set and we pay for
            # results only to discard them in the client-side slice below.
            "maxResults": max_results,
        }
        if from_date := options.get("LINKUP_FROM_DATE", ""):
            payload["fromDate"] = from_date
        if to_date := options.get("LINKUP_TO_DATE", ""):
            payload["toDate"] = to_date
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
        answer = ""
        if isinstance(data, dict) and output_type == "sourcedAnswer":
            answer = _text(data.get("answer"))
            rows = data.get("sources", [])
            snippet_field = "snippet"
        else:
            rows = data.get("results", []) if isinstance(data, dict) else []
            snippet_field = "content"
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _text(row.get("url"))
            if not url:
                continue
            text = _text(row.get(snippet_field))
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("name")),
                    url=url,
                    snippet=text[:_SNIPPET_CHARS],
                    # searchResults returns the full extracted page text in
                    # ``content``; keep it so the digest can use the fuller
                    # form rather than only the truncated snippet.
                    content=text if snippet_field == "content" and text else None,
                    published=None,
                )
            )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
            answer=answer or None,
        )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
