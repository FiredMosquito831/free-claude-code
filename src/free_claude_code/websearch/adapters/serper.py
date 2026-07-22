"""Serper adapter (POST google.serper.dev/search with X-API-KEY auth).

Result URLs live in the ``link`` field (not ``url``). Advanced dotenv options:
``SERPER_GL``/``SERPER_HL``/``SERPER_TBS`` request params and
``SERPER_RICH_BLOCKS`` (default on): answerBox -> response.answer with
knowledgeGraph and peopleAlsoAsk appended as context.
"""

from typing import Any, ClassVar

from free_claude_code.config.websearch_catalog import SERPER_DEFAULT_BASE
from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..options import option_enabled
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
        options = self._config.options
        payload: dict[str, Any] = {"q": query, "num": max_results}
        if gl := options.get("SERPER_GL", ""):
            payload["gl"] = gl
        if hl := options.get("SERPER_HL", ""):
            payload["hl"] = hl
        if tbs := options.get("SERPER_TBS", ""):
            payload["tbs"] = tbs
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/search",
            headers={"X-API-KEY": key},
            json_body=payload,
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
        answer = (
            _rich_blocks_answer(data)
            if isinstance(data, dict)
            and option_enabled(options.get("SERPER_RICH_BLOCKS"), default=True)
            else ""
        )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
            answer=answer or None,
        )


def _rich_blocks_answer(data: dict[str, Any]) -> str:
    """answerBox lead, then knowledgeGraph + peopleAlsoAsk context."""

    parts: list[str] = []
    answer_box = data.get("answerBox")
    if isinstance(answer_box, dict):
        text = _text(answer_box.get("answer")) or _text(answer_box.get("snippet"))
        if text:
            parts.append(text)
    knowledge_graph = data.get("knowledgeGraph")
    if isinstance(knowledge_graph, dict):
        title = _text(knowledge_graph.get("title"))
        description = _text(knowledge_graph.get("description"))
        if title and description:
            parts.append(f"{title}: {description}")
        elif title or description:
            parts.append(title or description)
    people_also_ask = data.get("peopleAlsoAsk")
    if isinstance(people_also_ask, list):
        for row in people_also_ask:
            if not isinstance(row, dict):
                continue
            question = _text(row.get("question"))
            snippet = _text(row.get("snippet"))
            if question and snippet:
                parts.append(f"Q: {question}\nA: {snippet}")
            elif question:
                parts.append(f"Q: {question}")
    return "\n\n".join(parts)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
