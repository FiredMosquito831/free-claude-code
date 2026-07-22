"""SerpAPI adapter (GET serpapi.com/search, api_key query param).

Result URLs live in the ``link`` field; error payloads can arrive with HTTP 200,
so the body is checked for a top-level ``error``. Advanced dotenv options:
``SERPAPI_ENGINE`` (google_light = smaller/cheaper payload)/``SERPAPI_TBS``/
``SERPAPI_GL``/``SERPAPI_HL`` request params; ``answer_box``/
``knowledge_graph`` are captured into ``response.answer`` when present.
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import SERPAPI_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..errors import WebSearchUpstreamError
from .http import build_async_client, request_json


class SerpApiWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "serpapi"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or SERPAPI_DEFAULT_BASE).rstrip("/")
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
        params: dict[str, Any] = {
            "engine": options.get("SERPAPI_ENGINE", "") or "google",
            "q": query,
            "api_key": key,
            "num": max_results,
        }
        if tbs := options.get("SERPAPI_TBS", ""):
            params["tbs"] = tbs
        if gl := options.get("SERPAPI_GL", ""):
            params["gl"] = gl
        if hl := options.get("SERPAPI_HL", ""):
            params["hl"] = hl
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "GET",
            f"{self._base_url}/search",
            params=params,
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
        answer = _answer_lead(data) if isinstance(data, dict) else ""
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
            answer=answer or None,
        )


def _answer_lead(data: dict[str, Any]) -> str:
    """answer_box lead, then knowledge_graph description as context."""

    parts: list[str] = []
    answer_box = data.get("answer_box")
    if isinstance(answer_box, dict):
        text = _text(answer_box.get("answer")) or _text(answer_box.get("snippet"))
        if text:
            parts.append(text)
    knowledge_graph = data.get("knowledge_graph")
    if isinstance(knowledge_graph, dict):
        title = _text(knowledge_graph.get("title"))
        description = _text(knowledge_graph.get("description"))
        if title and description:
            parts.append(f"{title}: {description}")
        elif title or description:
            parts.append(title or description)
    return "\n\n".join(parts)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
